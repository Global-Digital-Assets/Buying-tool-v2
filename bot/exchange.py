import os, math, time, datetime, asyncio
from typing import Dict, Dict as DictType, Optional
from enum import Enum
from binance import AsyncClient, enums
from dotenv import load_dotenv
from .logger import log_event, log_outcome
from .signal import fetch_signals
from .conf_decay import decayed_conf
from importlib import import_module

# Lazy import to avoid circular dependency at startup
def _calc_tp_pct(symbol: str, conf: float):
    return import_module("bot.risk").calc_tp_pct(symbol, conf)

# Load credentials; override any placeholder env
load_dotenv("/srv/futures-bot/.env", override=True)

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

# Fail-fast if creds not present; log partial keys for verification
if not API_KEY or not API_SECRET:
    raise RuntimeError("API credentials missing – check /srv/futures-bot/.env")
else:
    try:
        import asyncio
        # we can’t await log_event at import time; just print.
        print(f"CREDS_OK: KEY={API_KEY[:4]}*** SECRET={API_SECRET[:4]}***")
    except Exception:
        pass

TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
SL_PCT = float(os.getenv("SL_PCT", "0.15"))  # default fallback, not used in dynamic calc
SL_MIN_PCT = 0.01  # absolute minimum 1%
CLIENT_PREFIX = "BOT"

# Partial TP settings
PARTIAL_TP = os.getenv("PARTIAL_TP", "false").lower() == "true"
TP1_FRAC = float(os.getenv("TP1_FRAC", "0.5"))  # fraction to close at TP1

class PositionState(str, Enum):
    OPENING = "opening"
    ACTIVE = "active"
    REDUCING = "reducing"  # after TP1 hit
    CLOSING = "closing"
    CLOSED = "closed"

# simple in-memory state map {symbol: PositionState}
_STATE: DictType[str, PositionState] = {}
_ENTRY_PRICE: DictType[str, float] = {}
_ORIG_QTY: DictType[str, float] = {}

def _set_state(symbol: str, state: PositionState):
    _STATE[symbol] = state

def _get_state(symbol: str) -> Optional[PositionState]:
    return _STATE.get(symbol)

async def _client():
    return await AsyncClient.create(API_KEY, API_SECRET, testnet=TESTNET)

# ---------------- Helpers ----------------
async def get_wallet_balance(asset: str = "USDT") -> float:
    c = await _client()
    balances = await c.futures_account_balance()
    await c.close_connection()
    bal = next((b for b in balances if b["asset"] == asset), None)
    return float(bal["balance"]) if bal else 0.0


async def get_open_position(symbol: str):
    c = await _client()
    positions = await c.futures_position_information(symbol=symbol)
    await c.close_connection()
    return positions[0] if positions else None

async def get_margin_usage() -> float:
    """Return current total initial margin in USDT for all open futures positions"""
    c = await _client()
    acct = await c.futures_account()
    await c.close_connection()
    return float(acct.get("totalInitialMargin", 0.0))

    return positions[0] if positions else None

async def _round_qty(client, symbol: str, qty: float) -> float:
    info = await client.futures_exchange_info()
    sym_info = next(s for s in info["symbols"] if s["symbol"] == symbol)
    step = float(next(f for f in sym_info["filters"] if f["filterType"] == "LOT_SIZE")["stepSize"])
    precision = int(round(-math.log(step, 10), 0))
    return float(round(qty, precision))


async def _round_price(client, symbol: str, price: float) -> float:
    """Round price to nearest permissible tick size for the symbol."""
    info = await client.futures_exchange_info()
    sym_info = next(s for s in info["symbols"] if s["symbol"] == symbol)
    tick = float(next(f for f in sym_info["filters"] if f["filterType"] == "PRICE_FILTER")["tickSize"])
    precision = max(0, abs(int(round(math.log10(tick)))))
    rounded = round(round(price / tick) * tick, precision)
    return float(rounded)


# --- Direction Helper ---
_VALID_LONG_FLAGS = {"LONG", "BUY", "BULL", "BULLISH"}
_VALID_SHORT_FLAGS = {"SHORT", "SELL", "BEAR", "BEARISH"}

def _is_long(flag: str) -> bool:
    """Return True if the flag implies a long (buy) direction."""
    return (flag or "").upper() in _VALID_LONG_FLAGS

def _direction(flag: str):
    """Return 'LONG', 'SHORT', or None for unknown flag."""
    f = (flag or "").upper()
    if f in _VALID_LONG_FLAGS:
        return "LONG"
    if f in _VALID_SHORT_FLAGS:
        return "SHORT"
    return None

# ---------------- P&L helpers ----------------
async def get_today_realised_pnl(asset: str = "USDT") -> float:
    """Sum realised PnL since 00:00 UTC today."""
    client = await _client()
    start = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    income = await client.futures_income_history(startTime=int(start.timestamp()*1000), incomeType="REALIZED_PNL")
    await client.close_connection()
    return sum(float(i["income"]) for i in income if i.get("asset") == asset)

# ---------------- Core ----------------

async def place_order(signal, tier: Dict[str, float]):
    """Create entry plus protective TP/SL orders. Returns brief order info."""
    client = await _client()

    # Ensure leverage matches tier
    await client.futures_change_leverage(symbol=signal.symbol, leverage=tier["leverage"])

    # ----- sizing -----
    balance = await get_wallet_balance()
    margin_capital = balance * tier["pos_pct"]
    notional = margin_capital * tier["leverage"]
    mark_price = float((await client.futures_mark_price(symbol=signal.symbol))["markPrice"])
    qty = await _round_qty(client, signal.symbol, notional / mark_price)

    # store original entry details for ladder management
    _ENTRY_PRICE[signal.symbol] = mark_price
    _ORIG_QTY[signal.symbol] = qty

    dir_ = _direction(getattr(signal, "side", ""))
    if dir_ is None:
        await client.close_connection()
        raise ValueError(f"Unknown side flag: {getattr(signal, 'side', '')}")
    is_long = dir_ == "LONG"
    side = enums.SIDE_BUY if is_long else enums.SIDE_SELL
    opp_side = enums.SIDE_SELL if is_long else enums.SIDE_BUY

    client_id = f"{CLIENT_PREFIX}-{int(time.time()*1000)}"

    # prevent duplicate actions
    if _get_state(signal.symbol) in {PositionState.OPENING, PositionState.CLOSING}:
        await log_event("STATE_BLOCK", {"symbol": signal.symbol, "state": _get_state(signal.symbol)})
        await client.close_connection()
        return {}

    _set_state(signal.symbol, PositionState.OPENING)

    # ----- entry order -----
    if tier["order_type"] == "market":
        entry_resp = await client.futures_create_order(
            symbol=signal.symbol,
            side=side,
            type=enums.ORDER_TYPE_MARKET,
            quantity=qty,
            newClientOrderId=client_id,
        )
        entry_price = float(entry_resp.get("avgPrice") or mark_price)
    else:
        limit_price = await _round_price(client, signal.symbol, mark_price * (1 + tier["offset_pct"]))
        entry_resp = await client.futures_create_order(
            symbol=signal.symbol,
            side=side,
            type=enums.ORDER_TYPE_LIMIT,
            price=limit_price,
            quantity=qty,
            timeInForce="GTC",
            newClientOrderId=client_id,
        )
        entry_price = limit_price

    # Optional delay before protective orders
    delay_sec = int(os.getenv("PROTECTIVE_DELAY_SEC", "0"))
    if delay_sec > 0:
        await asyncio.sleep(delay_sec)

    # ----- compute protective prices -----
    tp_pct = _calc_tp_pct(signal.symbol, signal.confidence) / 100  # convert % → fraction
    sl_pct = max(SL_MIN_PCT, 2 * tp_pct)
    if is_long:
        sl_raw = entry_price * (1 - sl_pct)
        tp_raw = entry_price * (1 + tp_pct)
    else:
        sl_raw = entry_price * (1 + sl_pct)
        tp_raw = entry_price * (1 - tp_pct)

    sl_price = await _round_price(client, signal.symbol, sl_raw)
    tp_price = await _round_price(client, signal.symbol, tp_raw)

    # ----- protective orders -----
    errors: list[Exception] = []

    # SL as before (full closePosition)
    try:
        sl_resp = await client.futures_create_order(
            symbol=signal.symbol,
            side=opp_side,
            positionSide=("LONG" if is_long else "SHORT"),
            type=enums.FUTURE_ORDER_TYPE_STOP_MARKET,
            stopPrice=str(sl_price),
            closePosition=True,
            workingType="MARK_PRICE",
            newClientOrderId=f"{client_id}-SL",
        )
        await log_event("SL_OK", sl_resp)
    except Exception as e:
        errors.append(e)
        await log_event("ERROR_TP_SL", {"symbol": signal.symbol, "order": "SL", "error": str(e)})

    # ----- take-profit -----
    try:
        if PARTIAL_TP:
            qty_tp1 = await _round_qty(client, signal.symbol, qty * TP1_FRAC)
            tp_resp = await client.futures_create_order(
                symbol=signal.symbol,
                side=opp_side,
                positionSide=("LONG" if is_long else "SHORT"),
                type=enums.FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
                stopPrice=str(tp_price),
                quantity=qty_tp1,
                reduceOnly=True,
                workingType="MARK_PRICE",
                newClientOrderId=f"{client_id}-TP1",
            )
        else:
            tp_resp = await client.futures_create_order(
                symbol=signal.symbol,
                side=opp_side,
                positionSide=("LONG" if is_long else "SHORT"),
                type=enums.FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
                stopPrice=str(tp_price),
                closePosition=True,
                workingType="MARK_PRICE",
                newClientOrderId=f"{client_id}-TP",
            )
        await log_event("TP_OK", tp_resp)
    except Exception as e:
        errors.append(e)
        await log_event("ERROR_TP_SL", {"symbol": signal.symbol, "order": "TP", "error": str(e)})

    if errors:
        await client.close_connection()
        raise errors[0]

    await client.close_connection()

    _set_state(signal.symbol, PositionState.ACTIVE)
    return {"entry": entry_resp, "sl_price": sl_price, "tp_price": tp_price}

# ---------------- Order TTL ----------------
async def refresh_stale_orders(max_age_min: int = 15):
    """Cancel limit orders older than max_age_min; leave protective orders."""
    now_ms = int(time.time() * 1000)
    client = await _client()
    open_orders = await client.futures_get_open_orders()
    for o in open_orders:
        cid = o.get("clientOrderId", "")
        if not cid.startswith(CLIENT_PREFIX):
            continue
        if o["type"] != "LIMIT":
            continue
        age_min = (now_ms - int(o["updateTime"])) / 60000
        if age_min < max_age_min:
            continue
        try:
            await client.futures_cancel_order(symbol=o["symbol"], orderId=o["orderId"])
        except Exception:
            pass
    await client.close_connection()

# ---------------- TTL for positions remains ----------------
async def close_stale_positions(ttl_hours: int = 48):
    now_ms = int(time.time() * 1000)
    client = await _client()
    positions = await client.futures_position_information()
    for p in positions:
        amt = float(p.get("positionAmt", 0))
        if amt == 0:
            continue
        age_hours = (now_ms - int(p["updateTime"])) / 3_600_000
        if age_hours < ttl_hours:
            continue
        side = enums.SIDE_SELL if amt > 0 else enums.SIDE_BUY
        try:
            await client.futures_create_order(
                symbol=p["symbol"],
                side=side,
                type=enums.ORDER_TYPE_MARKET,
                closePosition=True,
            )
        except Exception:
            pass
    await client.close_connection()

# ---------------- Adaptive position management ----------------
async def manage_open_positions(
    ttl_hours: int = 6,
    flip_threshold: float = 0.6,
    min_conf: float = 0.4,
):
    """Close positions when:
    1) Age exceeds ttl_hours (hard stop)
    2) Latest signal flips direction with confidence ≥ flip_threshold
    3) Confidence decays below min_conf
    """
    now_ms = int(time.time() * 1000)

    # Fetch latest signals once for all symbols
    try:
        signals = await fetch_signals()
    except Exception:
        signals = []

    sig_map = {}
    for s in signals:
        dir_ = _direction(getattr(s, "side", ""))
        if dir_:
            sig_map[s.symbol] = (dir_, s.confidence)

    client = await _client()
    positions = await client.futures_position_information()

    for p in positions:
        amt = float(p.get("positionAmt", 0))
        if amt == 0:
            continue
        symbol = p["symbol"]
        side = "LONG" if amt > 0 else "SHORT"
        age_hours = (now_ms - int(p["updateTime"])) / 3_600_000

        should_close = False
        reason = ""

        # 1) Hard time-stop
        if age_hours >= ttl_hours:
            should_close = True
            reason = "TIME_STOP"
        else:
            sig = sig_map.get(symbol)
            if sig:
                sig_side, conf = sig
                # 2) Signal flip
                if sig_side != side and conf >= flip_threshold:
                    should_close = True
                    reason = "SIGNAL_FLIP"
                else:
                    # 3) Confidence decay
                    # check for TP1 hit if partial ladder enabled
                    if PARTIAL_TP and _get_state(symbol) == PositionState.ACTIVE:
                        orig_qty = _ORIG_QTY.get(symbol)
                        if orig_qty and abs(amt) <= orig_qty * (1 - TP1_FRAC + 0.05):
                            # TP1 assumed filled
                            await log_event("TP1_HIT", {"symbol": symbol})
                            try:
                                await client.futures_cancel_all_open_orders(symbol=symbol)
                            except Exception as e:
                                await log_event("ERROR_CANCEL", {"symbol": symbol, "error": str(e)})

                            # place breakeven SL for remaining qty
                            entry_price = _ENTRY_PRICE.get(symbol, 0.0)
                            if entry_price > 0:
                                breakeven = await _round_price(client, symbol, entry_price)
                                try:
                                    await client.futures_create_order(
                                        symbol=symbol,
                                        side=opp_side,
                                        positionSide=("LONG" if amt>0 else "SHORT"),
                                        type=enums.FUTURE_ORDER_TYPE_STOP_MARKET,
                                        stopPrice=str(breakeven),
                                        closePosition=True,
                                        workingType="MARK_PRICE",
                                        newClientOrderId=f"BE-SL-{int(time.time()*1000)}",
                                    )
                                    await log_event("BREAKEVEN_SL_SET", {"symbol": symbol, "price": breakeven})
                                    _set_state(symbol, PositionState.REDUCING)
                                except Exception as e:
                                    await log_event("ERROR_SL", {"symbol": symbol, "error": str(e)})

                    dec_conf = decayed_conf(conf, age_hours)
                    if dec_conf < min_conf:
                        should_close = True
                        reason = "CONF_DECAY"

        if not should_close:
            continue

        # guard duplicate closings
        if _get_state(symbol) == PositionState.CLOSING:
            continue
        _set_state(symbol, PositionState.CLOSING)

        # ----- Cancel existing protective orders to avoid race conditions -----
        try:
            await client.futures_cancel_all_open_orders(symbol=symbol)
            await asyncio.sleep(0.5)  # small buffer before closing
        except Exception as e:
            await log_event("ERROR_CANCEL", {"symbol": symbol, "error": str(e)})

        opp_side = enums.SIDE_SELL if amt > 0 else enums.SIDE_BUY
        try:
            await client.futures_create_order(
                symbol=symbol,
                side=opp_side,
                type=enums.ORDER_TYPE_MARKET,
                closePosition=True,
            )
        except Exception as e:
            await log_event("ERROR_CLOSE", {"symbol": symbol, "error": str(e)})

        # fetch exit price via mark price
        try:
            mp = await client.futures_mark_price(symbol=symbol)
            exit_price = float(mp.get("markPrice", 0.0))
        except Exception:
            exit_price = 0.0

        entry_price = float(p.get("entryPrice", 0))
        pnl_pct = (
            (exit_price - entry_price) / entry_price * 100 * (1 if amt > 0 else -1)
            if entry_price > 0 and exit_price > 0 else 0.0
        )

        await log_event("POSITION_CLOSED", {
            "symbol": symbol,
            "reason": reason,
            "age_h": round(age_hours, 2),
            "pnl_pct": round(pnl_pct, 2)
        })

        # outcome CSV
        await log_outcome({
            "ts": int(time.time()),
            "symbol": symbol,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl_percent": round(pnl_pct, 2),
            "hold_h": round(age_hours, 2),
            "reason": reason,
        })

        await client.close_connection()
        _set_state(symbol, PositionState.CLOSED)

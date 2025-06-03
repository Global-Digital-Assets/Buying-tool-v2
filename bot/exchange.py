import os, math, time
from typing import Dict
from binance import AsyncClient, enums

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
CLIENT_PREFIX = "BOT"

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



    dir_ = _direction(getattr(signal, "side", ""))
    if dir_ is None:
        await client.close_connection()
        raise ValueError(f"Unknown side flag: {getattr(signal, 'side', '')}")
    is_long = dir_ == "LONG"
    side = enums.SIDE_BUY if is_long else enums.SIDE_SELL
    opp_side = enums.SIDE_SELL if is_long else enums.SIDE_BUY

    client_id = f"{CLIENT_PREFIX}-{int(time.time()*1000)}"

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

    # ----- protective orders -----
    sl_raw = entry_price * (1 - tier["sl_pct"]) if side == enums.SIDE_BUY else entry_price * (1 + tier["sl_pct"])
    tp_raw = entry_price * (1 + tier["tp_pct"]) if side == enums.SIDE_BUY else entry_price * (1 - tier["tp_pct"])

    sl_price = await _round_price(client, signal.symbol, sl_raw)
    tp_price = await _round_price(client, signal.symbol, tp_raw)

    try:
        # Stop-loss (reduce-only)
        await client.futures_create_order(
            symbol=signal.symbol,
            side=opp_side,
            type=enums.ORDER_TYPE_STOP_MARKET,
            stopPrice=sl_price,
            closePosition=True,
            reduceOnly=True,
            newClientOrderId=f"{client_id}-SL",
        )
        # Take-profit limit (reduce-only)
        await client.futures_create_order(
            symbol=signal.symbol,
            side=opp_side,
            type=enums.ORDER_TYPE_LIMIT,
            price=tp_price,
            quantity=qty,
            timeInForce="GTC",
            reduceOnly=True,
            newClientOrderId=f"{client_id}-TP",
        )
    except Exception:
        pass  # tolerate failures (e.g. order size below min)

    await client.close_connection()
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


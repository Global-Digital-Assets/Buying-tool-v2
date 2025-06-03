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

# ---------------- Core ----------------
async def place_order(signal, tier: Dict[str, float]):
    client = await _client()

    await client.futures_change_leverage(symbol=signal.symbol, leverage=tier["leverage"])

    balance = await get_wallet_balance()
    margin_capital = balance * tier["pos_pct"]  # use % of wallet as margin
    notional = margin_capital * tier["leverage"]
    mark_price = float((await client.futures_mark_price(symbol=signal.symbol))["markPrice"])
    qty = await _round_qty(client, signal.symbol, notional / mark_price)

    side = enums.SIDE_BUY if getattr(signal, "side", "LONG").upper() == "LONG" else enums.SIDE_SELL
    opp_side = enums.SIDE_SELL if side == enums.SIDE_BUY else enums.SIDE_BUY

    new_client_id = f"{CLIENT_PREFIX}-{int(time.time()*1000)}"

    if tier["order_type"] == "market":
        entry = await client.futures_create_order(
            symbol=signal.symbol,
            side=side,
            type=enums.ORDER_TYPE_MARKET,
            quantity=qty,
            newClientOrderId=new_client_id,
        )
    else:
        offset = tier["offset_pct"]
        price = mark_price * (1 + offset)
        price = float(round(price, 2))
        entry = await client.futures_create_order(
            symbol=signal.symbol,
            side=side,
            type=enums.ORDER_TYPE_LIMIT,
            quantity=qty,
            price=price,
            timeInForce="GTC",
            newClientOrderId=new_client_id,
        )

    # Protective orders (reduce-only) placed regardless; they will stay pending until position opens.
    sl_price = mark_price * (1 - tier["sl_pct"]) if side == enums.SIDE_BUY else mark_price * (1 + tier["sl_pct"])
    sl_price = float(round(sl_price, 2))
    tp_price = mark_price * (1 + tier["tp_pct"]) if side == enums.SIDE_BUY else mark_price * (1 - tier["tp_pct"])
    tp_price = float(round(tp_price, 2))

    try:
        await client.futures_create_order(
            symbol=signal.symbol,
            side=opp_side,
            type=enums.ORDER_TYPE_STOP_MARKET,
            stopPrice=sl_price,
            closePosition=True,
            reduceOnly=True,
            newClientOrderId=f"{new_client_id}-SL",
        )
        await client.futures_create_order(
            symbol=signal.symbol,
            side=opp_side,
            type=enums.ORDER_TYPE_LIMIT,
            quantity=qty,
            price=tp_price,
            timeInForce="GTC",
            reduceOnly=True,
            newClientOrderId=f"{new_client_id}-TP",
        )
    except Exception:
        pass  # tolerate if they fail due to no position yet

    await client.close_connection()
    return entry

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

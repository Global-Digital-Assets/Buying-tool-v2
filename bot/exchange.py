import os, math, asyncio, time
from typing import Dict
from binance import AsyncClient, enums

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"

async def _client():
    return await AsyncClient.create(API_KEY, API_SECRET, testnet=TESTNET)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def get_wallet_balance(asset: str = "USDT") -> float:
    client = await _client()
    balances = await client.futures_account_balance()
    await client.close_connection()
    bal = next((b for b in balances if b["asset"] == asset), None)
    return float(bal["balance"]) if bal else 0.0

async def get_open_position(symbol: str):
    client = await _client()
    positions = await client.futures_position_information(symbol=symbol)
    await client.close_connection()
    return positions[0] if positions else None

async def _round_qty(client, symbol: str, qty: float) -> float:
    info = await client.futures_exchange_info()
    sym_info = next(s for s in info["symbols"] if s["symbol"] == symbol)
    step = float(next(f for f in sym_info["filters"] if f["filterType"] == "LOT_SIZE")["stepSize"])
    precision = int(round(-math.log(step, 10), 0))
    return float(round(qty, precision))

# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------
async def place_order(signal, tier: Dict[str, float]):
    """Market entry + TP limit + SL stop according to tier settings."""
    client = await _client()

    # 1. Leverage
    await client.futures_change_leverage(symbol=signal.symbol, leverage=tier["leverage"])

    # 2. Size (slice of full wallet balance)
    balance = await get_wallet_balance()
    notional = balance * tier["pos_pct"]
    mark_price = float((await client.futures_mark_price(symbol=signal.symbol))["markPrice"])
    qty = await _round_qty(client, signal.symbol, notional / mark_price)

    side = enums.SIDE_BUY if getattr(signal, "side", "LONG").upper() == "LONG" else enums.SIDE_SELL
    opp_side = enums.SIDE_SELL if side == enums.SIDE_BUY else enums.SIDE_BUY

    # 3. Entry order
    entry = await client.futures_create_order(
        symbol=signal.symbol,
        side=side,
        type=enums.ORDER_TYPE_MARKET,
        quantity=qty,
    )

    # 4. Stop-loss (reduce-only)
    sl_price = mark_price * (1 - tier["sl_pct"]) if side == enums.SIDE_BUY else mark_price * (1 + tier["sl_pct"])
    sl_price = float(round(sl_price, 2))
    await client.futures_create_order(
        symbol=signal.symbol,
        side=opp_side,
        type=enums.ORDER_TYPE_STOP_MARKET,
        stopPrice=sl_price,
        closePosition=True,
        reduceOnly=True,
    )

    # 5. Take-profit limit (reduce-only)
    tp_price = mark_price * (1 + tier["tp_pct"]) if side == enums.SIDE_BUY else mark_price * (1 - tier["tp_pct"])
    tp_price = float(round(tp_price, 2))
    await client.futures_create_order(
        symbol=signal.symbol,
        side=opp_side,
        type=enums.ORDER_TYPE_LIMIT,
        price=tp_price,
        quantity=qty,
        timeInForce="GTC",
        reduceOnly=True,
    )

    await client.close_connection()
    return entry

# ---------------------------------------------------------------------------
# TTL Enforcement
# ---------------------------------------------------------------------------
async def close_stale_positions(ttl_hours: int = 48):
    """Market-close positions older than ttl_hours."""
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
            pass  # swallow and continue
    await client.close_connection()

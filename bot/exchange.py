import os, math, asyncio
from binance import AsyncClient, enums

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"

async def _client():
    return await AsyncClient.create(API_KEY, API_SECRET, testnet=TESTNET)

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
    step_size = float(next(f for f in sym_info["filters"] if f["filterType"] == "LOT_SIZE")["stepSize"])
    precision = int(round(-math.log(step_size, 10), 0))
    return float(round(qty, precision))

async def place_order(signal):
    """Place a market order with immediate stop-loss."""
    client = await _client()

    leverage = int(float(os.getenv("MAX_LEVERAGE", "5")))
    await client.futures_change_leverage(symbol=signal.symbol, leverage=leverage)

    balance = await get_wallet_balance()
    notional = balance * float(os.getenv("POS_SIZE_PCT", "0.15"))
    mark_price = float((await client.futures_mark_price(symbol=signal.symbol))["markPrice"])
    qty = notional / mark_price
    qty = await _round_qty(client, signal.symbol, qty)

    side = enums.SIDE_BUY if signal.side.upper() == "LONG" else enums.SIDE_SELL
    opp_side = enums.SIDE_SELL if side == enums.SIDE_BUY else enums.SIDE_BUY

    order = await client.futures_create_order(
        symbol=signal.symbol,
        side=side,
        type=enums.ORDER_TYPE_MARKET,
        quantity=qty,
    )

    sl_pct = float(os.getenv("STOP_LOSS_PCT", "0.15"))
    sl_price = mark_price * (1 - sl_pct) if side == enums.SIDE_BUY else mark_price * (1 + sl_pct)
    sl_price = float(round(sl_price, 2))

    await client.futures_create_order(
        symbol=signal.symbol,
        side=opp_side,
        type=enums.ORDER_TYPE_STOP_MARKET,
        stopPrice=sl_price,
        closePosition=True,
    )

    await client.close_connection()
    return order

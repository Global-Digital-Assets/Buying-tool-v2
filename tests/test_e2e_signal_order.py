import pytest

from bot import main as bot_main
from bot.signal import Signal

@ pytest.mark.asyncio
async def test_trade_cycle_places_tp_sl(monkeypatch):
    # Patch signal source
def fake_fetch_signals(limit=50):
        return [Signal(symbol="TESTUSDT", price=100.0, volume=1.0, timestamp=1,
                       confidence=0.95, side="LONG")]
    monkeypatch.setattr("bot.signal.fetch_signals", fake_fetch_signals)

    # Patch risk checks and balances
    async def fake_get_open_position(symbol):
        return None
    monkeypatch.setattr("bot.exchange.get_open_position", fake_get_open_position)

    async def fake_get_wallet_balance(asset="USDT"):
        return 1000.0
    monkeypatch.setattr("bot.exchange.get_wallet_balance", fake_get_wallet_balance)

    async def fake_get_margin_usage():
        return 0.0
    monkeypatch.setattr("bot.exchange.get_margin_usage", fake_get_margin_usage)

    # Patch client for order recording
    class DummyClient:
        def __init__(self):
            self.orders = []
        async def futures_change_leverage(self, symbol, leverage):
            pass
        async def futures_mark_price(self, symbol):
            return {"markPrice": "100"}
        async def futures_create_order(self, **kwargs):
            self.orders.append(kwargs)
            return {"avgPrice": "100"}
        async def close_connection(self):
            pass

    dummy = DummyClient()
    async def fake_client():
        return dummy
    monkeypatch.setattr("bot.exchange._client", fake_client)

    # Patch rounding helpers
    monkeypatch.setattr("bot.exchange._round_qty", lambda client, s, q: q)
    monkeypatch.setattr("bot.exchange._round_price", lambda client, s, p: p)

    # Execute one trade cycle
    await bot_main.trade_cycle()

    # Verify orders: entry + SL + TP
    types = [o.get("type") for o in dummy.orders]
    assert any(t == 'STOP_MARKET' for t in types)
    assert any(t == 'TAKE_PROFIT_MARKET' for t in types)
    assert len(dummy.orders) == 3

import pytest
from bot.exchange import _direction

@pytest.mark.parametrize("flag,expected",[
    ("BUY", "LONG"),
    ("buy", "LONG"),
    ("LONG", "LONG"),
    ("SELL", "SHORT"),
    ("short", "SHORT"),
    ("BEARISH", "SHORT"),
    ("foo", None),
])
def test_direction_flags(flag, expected):
    assert _direction(flag) == expected

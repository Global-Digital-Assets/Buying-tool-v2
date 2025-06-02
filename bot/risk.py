"""Risk management and execution parameters per confidence tier."""
import os
from typing import Optional, Dict
from .exchange import get_wallet_balance, get_open_position

# (lo, hi, leverage, pos_pct, tp_pct, order_type, offset_pct, ttl_min)
_TIERS = [
    (0.95, 1.00, 10, 0.20, 0.030, "market", 0.0, 0),
    (0.90, 0.9499, 10, 0.20, 0.030, "limit", -0.0035, 3),
    (0.80, 0.8999, 7, 0.15, 0.025, "limit", -0.0050, 10),
    (0.70, 0.7999, 5, 0.10, 0.020, "limit", -0.0100, 15),
    (0.60, 0.6999, 3, 0.05, 0.015, "limit", -0.0150, 15),
]

STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.15"))

async def _select_tier(conf: float) -> Optional[Dict[str, float]]:
    for lo, hi, lev, pos, tp, otype, offset, ttl in _TIERS:
        if lo <= conf <= hi:
            return {
                "leverage": lev,
                "pos_pct": pos,
                "tp_pct": tp,
                "sl_pct": STOP_LOSS_PCT,
                "order_type": otype,
                "offset_pct": offset,
                "ttl_min": ttl,
            }
    return None

async def risk_check(signal):
    tier = await _select_tier(signal.confidence)
    if not tier:
        return None
    pos = await get_open_position(signal.symbol)
    if pos and abs(float(pos.get("positionAmt", 0))) != 0:
        return None
    if await get_wallet_balance() <= 0:
        return None
    return tier

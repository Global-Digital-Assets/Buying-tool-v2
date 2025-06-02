"""Risk management: confidence-tier mapping and basic sanity checks.
Returns tier parameters if a trade is allowed, otherwise None.
"""
import os
from typing import Optional, Dict
from .exchange import get_wallet_balance, get_open_position

# (lo, hi, leverage, pos_size %, tp %)
_TIERS = [
    (0.60, 0.69, 3, 0.05, 0.015),
    (0.70, 0.79, 5, 0.10, 0.020),
    (0.80, 0.89, 7, 0.15, 0.025),
    (0.90, 1.00, 10, 0.20, 0.030),
]

STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.15"))

async def _select_tier(conf: float) -> Optional[Dict[str, float]]:
    for lo, hi, lev, pct, tp in _TIERS:
        if lo <= conf <= hi:
            return {
                "leverage": lev,
                "pos_pct": pct,
                "tp_pct": tp,
                "sl_pct": STOP_LOSS_PCT,
            }
    return None

async def risk_check(signal):
    """Return tier dict or None if risk checks fail."""
    tier = await _select_tier(signal.confidence)
    if not tier:
        return None

    # No duplicate positions per symbol
    pos = await get_open_position(signal.symbol)
    if pos and abs(float(pos.get("positionAmt", 0))) > 0:
        return None

    # Require positive wallet balance
    if await get_wallet_balance() <= 0:
        return None

    return tier

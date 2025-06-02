import os
from .exchange import get_wallet_balance, get_open_position

async def risk_check(signal):
    """Return True if the trade passes static risk checks."""
    if os.getenv("RISK_ENABLED", "true").lower() != "true":
        return True

    # Confidence gate
    threshold = float(os.getenv("CONF_THRESHOLD", "0"))
    if signal.confidence < threshold:
        return False

    # Block if a position is already open
    pos = await get_open_position(signal.symbol)
    if pos and abs(float(pos["positionAmt"])) > 0:
        return False

    # Basic wallet sanity
    if await get_wallet_balance() <= 0:
        return False

    return True

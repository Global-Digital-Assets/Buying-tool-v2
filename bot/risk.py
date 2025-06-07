"""Risk management and execution parameters per confidence tier."""
import os
from typing import Optional, Dict
from pathlib import Path
from math import sqrt, exp
import yaml  # type: ignore

# (lo, hi, leverage, pos_pct, tp_pct, order_type, offset_pct, ttl_min)
_TIERS = [
    (0.95, 1.0, 10, 0.12, 0.03, 'market', 0.0, 0),
    (0.9, 0.9499, 10, 0.09, 0.025, 'limit', -0.0015, 3),
    (0.85, 0.8999, 7, 0.07, 0.021, 'limit', -0.003, 10),
    (0.8, 0.8499, 7, 0.07, 0.018, 'limit', -0.005, 10),
    (0.7, 0.7999, 5, 0.05, 0.016, 'limit', -0.0075, 10),
    (0.6, 0.6999, 3, 0.03, 0.013, 'limit', -0.01, 15),
]

STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.15"))

# ---------------- Dynamic TP & Confidence Decay -----------------
_CFG_PATH = Path(__file__).resolve().parent.parent / "config" / "token_buckets.yaml"

def _load_token_buckets() -> Dict[str, float]:
    """Return mapping TOKEN -> tp_base% from YAML.  Falls back to empty dict."""
    buckets: Dict[str, float] = {}
    if _CFG_PATH.exists():
        try:
            cfg = yaml.safe_load(_CFG_PATH.read_text()) or {}
            for grp in cfg.get("volatility_groups", {}).values():
                base = float(grp.get("tp_base", 2.0))
                for tok in grp.get("tokens", []):
                    buckets[tok.upper()] = base
        except Exception:
            # Minimal fallback parser (handles the simple structure we use)
            current_base = 2.0
            for line in _CFG_PATH.read_text().splitlines():
                s = line.strip()
                if s.startswith("tp_base:"):
                    current_base = float(s.split(":", 1)[1])
                elif s.startswith("tokens:"):
                    inner = s.split("[", 1)[1].split("]", 1)[0]
                    for tok in inner.split(","):
                        buckets[tok.strip().upper()] = current_base
    return buckets

_TOKEN_BUCKETS = _load_token_buckets()

# Defaults / tunables (env-overrideable)
DEFAULT_TP = float(os.getenv("DEFAULT_TP", "2.0"))  # percent
MIN_TP = float(os.getenv("MIN_TP", "0.3"))          # percent
DECAY_K = float(os.getenv("DECAY_K", "0.30"))      # h⁻¹  (half-life ~2.3 h)

def calc_tp_pct(symbol: str, confidence: float, hold_hours: int = 3) -> float:
    """Return take-profit % (e.g. 1.75 for 1.75 %).

    Logic: base % from volatility bucket × (0.8–1.2 depending on confidence) × √t.
    Ensures TP never below MIN_TP.
    """
    token = symbol.replace("USDT", "").upper()
    base = _TOKEN_BUCKETS.get(token, DEFAULT_TP)

    # 0.0 ≤ confidence ≤ 1.0  →  0.8–1.2 multiplier
    conf_mult = 0.8 + confidence * 0.4
    time_mult = sqrt(max(hold_hours, 1) / 3.0)

    tp = base * conf_mult * time_mult
    return max(round(tp, 2), MIN_TP)

def decay_confidence(initial_conf: float, age_hours: float) -> float:
    """Exponential decay of confidence over time (hours)."""
    return initial_conf * exp(-DECAY_K * age_hours)

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
    """Return tier purely based on confidence; NO other risk gates."""
    return await _select_tier(signal.confidence)

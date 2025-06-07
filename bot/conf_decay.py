"""Confidence decay helper: apply exponential decay after N hours."""
import math

def decayed_conf(conf: float, hours: float, half_life_h: float = 6.0) -> float:
    """Return decayed confidence after <hours> elapsed."""
    decay_factor = 0.5 ** (hours / half_life_h)
    return round(conf * decay_factor, 3)

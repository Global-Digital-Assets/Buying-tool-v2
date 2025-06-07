import os, httpx, asyncio
from pydantic import BaseModel, Field, ValidationError
from typing import List, Optional, Union

class Signal(BaseModel):
    symbol: str
    price: float
    volume: float
    timestamp: Union[int, str]
    confidence: float
    side: str = Field(default="LONG")  # analytics feed currently single-direction

aSYNC_TIMEOUT=5

def _endpoint():
    """Return analytics signal endpoint URL.

    Respects the SIGNAL_URL environment variable if set; otherwise defaults to the
    local analytics service (same VPS) at ``http://127.0.0.1:8080/api/signals``.
    This prevents silent failures when the env var is missing and ensures the bot
    always has a working endpoint.
    """
    return os.getenv("SIGNAL_URL", "http://127.0.0.1:8080/api/signals")

def _threshold():
    return float(os.getenv("CONF_THRESHOLD", "0.60"))

async def fetch_signals(limit:int=50) -> List[Signal]:
    """Return all signals >= threshold sorted high→low confidence (max `limit`)."""
    url=_endpoint()
    if not url:
        return []
    try:
        async with httpx.AsyncClient(timeout=aSYNC_TIMEOUT) as client:
            resp=await client.get(url)
        resp.raise_for_status()
        data=resp.json()
        if not isinstance(data, list):
            return []
        valid: List[Signal]=[]
        th=_threshold()
        for item in data:
            try:
                sig=Signal.model_validate(item)
                if sig.confidence>=th:
                    valid.append(sig)
            except ValidationError:
                continue
        valid.sort(key=lambda s: s.confidence, reverse=True)
        return valid[:limit]
    except Exception:
        return []

async def fetch_signal() -> Optional[Signal]:
    """Back-compat: return just top signal or None"""
    sigs=await fetch_signals(limit=1)
    return sigs[0] if sigs else None

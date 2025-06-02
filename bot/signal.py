import os, httpx, asyncio
from pydantic import BaseModel, Field, ValidationError
from typing import List, Optional

class Signal(BaseModel):
    symbol: str
    price: float
    volume: float
    timestamp: int
    confidence: float
    side: str = Field(default="LONG")  # analytics feed currently single-direction

async def fetch_signal() -> Optional[Signal]:
    url = os.getenv("SIGNAL_URL")
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return None
        # choose highest-confidence item ≥ threshold
        threshold = float(os.getenv("CONF_THRESHOLD", "0.0"))
        candidates: List[Signal] = []
        for item in data:
            try:
                sig = Signal.model_validate(item)
                if sig.confidence >= threshold:
                    candidates.append(sig)
            except ValidationError:
                continue
        if not candidates:
            return None
        candidates.sort(key=lambda s: s.confidence, reverse=True)
        return candidates[0]
    except Exception:
        return None

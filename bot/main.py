import os, asyncio
from fastapi import FastAPI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from .signal import fetch_signal
from .risk import risk_check
from .exchange import place_order, close_stale_positions
from .logger import log_event
import uvicorn

app = FastAPI()
trading_enabled = True

async def trade_cycle():
    if not trading_enabled:
        return
    try:
        signal = await fetch_signal()
        if not signal:
            return
        tier = await risk_check(signal)
        if not tier:
            return
        order = await place_order(signal, tier)
        await log_event("ORDER_PLACED", order)
    except Exception as e:
        await log_event("ERROR", str(e))

@app.on_event("startup")
async def startup():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(trade_cycle, "interval", minutes=15)
    ttl_hours = int(os.getenv("TTL_HOURS", "48"))
    scheduler.add_job(close_stale_positions, "interval", hours=1, kwargs={"ttl_hours": ttl_hours})
    scheduler.start()

@app.get("/health")
async def health():
    return {"status": "OK", "trading_enabled": trading_enabled}

@app.post("/halt")
async def halt():
    global trading_enabled
    trading_enabled = False
    return {"status": "HALTED"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

import os
from fastapi import FastAPI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from .signal import fetch_signals
from .risk import risk_check
from .exchange import place_order, close_stale_positions, refresh_stale_orders
from .logger import log_event
import uvicorn

app = FastAPI()
trading_enabled = True
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

async def trade_cycle():
    if not trading_enabled:
        return
    try:
        signals = await fetch_signals()
        if DEBUG and signals:
            await log_event("DEBUG_SIGNALS", [s.model_dump() for s in signals])
        if not signals:
            return
        opened = 0
        for signal in signals:
            tier = await risk_check(signal)
            if DEBUG and tier:
                await log_event("DEBUG_TIER", {"symbol": signal.symbol, **tier})
            if not tier:
                continue
            order = await place_order(signal, tier)
            await log_event("ORDER_PLACED", order)
            opened += 1
            if opened >= 10:
                break
        if signal and DEBUG:
            await log_event("DEBUG_SIGNAL", signal.model_dump())
        if not signal:
            return
        tier = await risk_check(signal)
        if DEBUG:
            await log_event("DEBUG_TIER", tier)
        if not tier:
            return
        order = await place_order(signal, tier)
        await log_event("ORDER_PLACED", order)
    except Exception as e:
        await log_event("ERROR", str(e))

@app.on_event("startup")
async def startup():
    sched = AsyncIOScheduler()
    sched.add_job(trade_cycle, "interval", minutes=15)
    ttl_hours = int(os.getenv("TTL_HOURS", "48"))
    sched.add_job(close_stale_positions, "interval", hours=1, kwargs={"ttl_hours": ttl_hours})
    sched.add_job(refresh_stale_orders, "interval", minutes=5)  # cancel stale limits every 5 min
    sched.start()

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
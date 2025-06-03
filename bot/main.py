import os
from fastapi import FastAPI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from .signal import fetch_signals
from .risk import risk_check
from .exchange import place_order, _direction, close_stale_positions, refresh_stale_orders, get_wallet_balance, get_margin_usage
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
        if not signals:
            return
        balance = await get_wallet_balance()
        margin_cap_pct = float(os.getenv("MARGIN_CAP_PCT", "0.70"))
        margin_cap = balance * margin_cap_pct
        current_margin = await get_margin_usage()
        available_margin = margin_cap - current_margin
        if available_margin <= 0:
            if DEBUG:
                await log_event("MARGIN_CAP_HIT", {"margin_cap": margin_cap, "current_margin": current_margin})
            return
        if DEBUG:
            await log_event("DEBUG_SIGNALS", [s.model_dump() for s in signals])
        opened = 0
        for signal in signals:
            dir_ = _direction(getattr(signal, "side", ""))
            if dir_ is None:
                if DEBUG:
                    await log_event("SKIP_UNKNOWN_SIDE", signal.model_dump())
                continue
            tier = await risk_check(signal)
            if not tier:
                continue
            required_margin = balance * tier["pos_pct"]
            if required_margin > available_margin:
                continue
            if DEBUG:
                await log_event("DEBUG_TIER", {"symbol": signal.symbol, **tier})
            order = await place_order(signal, tier)
            await log_event("ORDER_PLACED", order)
            available_margin -= required_margin
            opened += 1
            if opened >= 10 or available_margin <= 0:
                break
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
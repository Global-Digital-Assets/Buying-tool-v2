import os
import uvicorn
from fastapi import FastAPI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timezone

from .signal import fetch_signals
from .risk import risk_check
from .exchange import (
    place_order,
    _direction,
    close_stale_positions,
    refresh_stale_orders,
    get_wallet_balance,
    get_margin_usage,
    get_today_realised_pnl,
    manage_open_positions
)
from .logger import log_event

app = FastAPI()

trading_enabled = True
DEBUG = os.getenv("DEBUG", "false").lower() == "true"


async def trade_cycle():
    if not trading_enabled:
        return

    from datetime import datetime, timezone
    opened = 0
    await log_event("CYCLE_START", datetime.now(timezone.utc).isoformat())

    try:
        signals = await fetch_signals()
        if not signals:
            return
        balance = await get_wallet_balance()
        # Skip daily loss cap & margin cap checks – aggressive execution mode
        available_margin = balance
        if DEBUG:
            await log_event("DEBUG_SIGNALS", [s.model_dump() for s in signals])
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
            # skip margin cap gate – execute regardless
            if DEBUG:
                await log_event("DEBUG_TIER", {"symbol": signal.symbol, **tier})
            order = await place_order(signal, tier)
            await log_event("ORDER_PLACED", order)
            available_margin -= required_margin
            opened += 1
            if opened >= 10:
                break
    except Exception as e:
        await log_event("ERROR", str(e))
    finally:
        await log_event("CYCLE_DONE", {"opened": opened})

@app.on_event("startup")
async def startup():
    import asyncio
    # Scheduler pinned to UTC
    sched = AsyncIOScheduler(timezone=timezone.utc)
    sched.add_job(
        trade_cycle,
        "interval",
        minutes=15,
        max_instances=1,
    )
    ttl_hours = int(os.getenv("TTL_HOURS", "6"))  # 6-hour hard time-stop
    sched.add_job(close_stale_positions, "interval", hours=1, kwargs={"ttl_hours": ttl_hours}, max_instances=1)
    sched.add_job(manage_open_positions, "interval", hours=1, max_instances=1)
    sched.add_job(refresh_stale_orders, "interval", minutes=5)  # cancel stale limits every 5 min
    sched.start()

    # run first cycle immediately
    asyncio.create_task(trade_cycle())

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

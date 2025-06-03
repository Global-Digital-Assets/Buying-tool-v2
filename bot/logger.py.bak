import os, json, aiosqlite, httpx
from datetime import datetime, timezone

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
DB_PATH = os.path.join(DATA_DIR, "trades.sqlite")

TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

async def _ensure_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                type TEXT,
                payload TEXT
            )""")
        await db.commit()

async def _send_tg(text: str):
    if not (TG_TOKEN and TG_CHAT):
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(url, data={"chat_id": TG_CHAT, "text": text})

async def log_event(event_type: str, payload):
    await _ensure_db()
    ts = datetime.now(timezone.utc).isoformat()
    data = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO logs(ts, type, payload) VALUES(?,?,?)", (ts, event_type, data))
        await db.commit()

    if event_type in {"ERROR", "WARN"}:
        await _send_tg(f"{event_type}: {data}")

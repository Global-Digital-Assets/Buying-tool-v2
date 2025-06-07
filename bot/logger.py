import os, json, aiosqlite, httpx, csv, asyncio
from datetime import datetime, timezone

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
DB_PATH = os.path.join(DATA_DIR, "trades.sqlite")
CSV_OUT = os.path.join(DATA_DIR, "outcomes.csv")

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

# ---------------- outcome CSV ----------------
def _ensure_csv():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(CSV_OUT):
        with open(CSV_OUT, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["ts", "symbol", "entry_price", "exit_price", "pnl_percent", "hold_h", "reason"])

async def log_outcome(row: dict):
    """Append trade outcome to CSV synchronously (called from async context)."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _write_csv_row, row)

def _write_csv_row(row: dict):
    _ensure_csv()
    with open(CSV_OUT, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            row.get("ts"),
            row.get("symbol"),
            row.get("entry_price"),
            row.get("exit_price"),
            row.get("pnl_percent"),
            row.get("hold_h"),
            row.get("reason"),
        ])

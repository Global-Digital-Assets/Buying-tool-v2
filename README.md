# Futures-Bot

Minimal Binance futures executor (3-10×) — auto-deploy via Docker.

## Quick start
```bash
cp config/.env.example .env  # fill keys
docker build -t futures-bot .
docker run -d --restart=always --env-file .env -p 8000:8000 futures-bot
```

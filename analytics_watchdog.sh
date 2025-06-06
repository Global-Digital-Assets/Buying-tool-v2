#!/usr/bin/env bash

# Analytics & Futures-Bot Watchdog (hardened)
# - Retries status calls
# - Requires N consecutive failures before alerting
# - Uses longer curl timeouts to avoid false positives

set -euo pipefail

LOG_FILE="/srv/futures-bot/logs/watchdog.log"
mkdir -p "$(dirname "$LOG_FILE")"

API_URL="http://localhost:8080/api/analysis"
HEALTH_URL="http://localhost:8080/api/status"
BOT_HEALTH="http://localhost:8000/health"

TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

MAX_SIGNAL_AGE=900   # 15-min
STATE_FILE="/tmp/analytics_failcount"
MAX_FAILS=3          # require 3 consecutive bad runs (~6 min)

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') [WATCHDOG] $*" | tee -a "$LOG_FILE"
}

alert() {
  local msg="$1"
  log "ALERT: $msg"
  if [[ -n "$TELEGRAM_BOT_TOKEN" && -n "$TELEGRAM_CHAT_ID" ]]; then
    curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      -d "chat_id=${TELEGRAM_CHAT_ID}" \
      -d "text=🚨 FUTURES BOT ALERT: $msg" \
      -d "parse_mode=HTML" >/dev/null 2>&1 || true
  fi
}

reset_failcount() {
  echo 0 > "$STATE_FILE"
}

inc_failcount() {
  local c=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
  c=$((c+1))
  echo "$c" > "$STATE_FILE"
  echo "$c"
}

check_status() {
  # ---------- STATUS ENDPOINT ----------
  local status_json
  if ! status_json=$(curl -sS --fail --retry 3 --retry-delay 2 --max-time 10 "$HEALTH_URL" 2>/dev/null); then
    local fails=$(inc_failcount)
    if (( fails >= MAX_FAILS )); then
      alert "Analytics API status endpoint is DOWN (fails=${fails})"
    else
      log "status endpoint failure (${fails}/${MAX_FAILS}) – no alert yet"
    fi
    return 1
  fi
  # success: reset counter if previously failing
  local prev=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
  if (( prev >= MAX_FAILS )); then
    alert "Analytics API recovered"
  fi
  reset_failcount

  local status_field=$(echo "$status_json" | jq -r '.status // empty')
  if [[ "$status_field" != "operational" ]]; then
    alert "Analytics API status not operational: $status_field"
    return 1
  fi

  # ---------- ANALYSIS ENDPOINT ----------
  local analysis_json
  if ! analysis_json=$(curl -sS --fail --retry 3 --retry-delay 2 --max-time 10 "$API_URL" 2>/dev/null); then
    alert "Analytics API /analysis endpoint is DOWN"
    return 1
  fi

  local ts=$(echo "$analysis_json" | jq -r '.timestamp // empty')
  if [[ -z "$ts" ]]; then
    alert "Analytics API response missing timestamp"
    return 1
  fi
  local age=$(( $(date +%s) - $(date -d "$ts" +%s 2>/dev/null || echo 0) ))
  if (( age > MAX_SIGNAL_AGE )); then
    alert "Analytics API signals are STALE ($((age/60)) min)"
    return 1
  fi
  log "Analytics API healthy – signals age: $((age/60)) min"
  return 0
}

check_bot() {
  if ! bot_json=$(curl -sS --fail --retry 3 --retry-delay 2 --max-time 8 "$BOT_HEALTH" 2>/dev/null); then
    alert "Futures Bot health endpoint not responding"
    systemctl restart futures-bot || true
    return 1
  fi
  local st=$(echo "$bot_json" | jq -r '.status // empty')
  if [[ "$st" != "OK" ]]; then
    alert "Futures Bot status warning: $st"
    return 1
  fi
  log "Futures Bot healthy"
}

main() {
  log "---- watchdog run ----"
  check_status && status_ok=1 || status_ok=0
  check_bot && bot_ok=1 || bot_ok=0
  if (( status_ok && bot_ok )); then
    log "All systems operational"
  else
    log "Issues detected – see alerts above"
  fi
}

main "$@"

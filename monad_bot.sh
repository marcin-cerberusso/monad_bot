#!/bin/bash
# ============================================================================
# MONAD BOT - Auto-restart supervisor script
# ============================================================================

set -e

# === CONFIGURATION ===
BOT_DIR="/home/marcin/windsurf/copydevawwallet/solana-trading-bot/monad_engine"
LOG_DIR="${BOT_DIR}/logs"
VENV_PATH="${BOT_DIR}/.venv"
MAX_RESTARTS=10
RESTART_DELAY=5
RESTART_COUNT=0

# === SETUP ===
mkdir -p "$LOG_DIR"
cd "$BOT_DIR"

# Security check
if [ -f ".env" ]; then
    chmod 600 .env
fi

# === LOGGING FUNCTION ===
log() {
    local level="$1"
    shift
    local message="$*"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[${timestamp}] [${level}] ${message}" | tee -a "${LOG_DIR}/supervisor.log"
}

# === SIGNAL HANDLERS ===
cleanup() {
    log "INFO" "Received shutdown signal, stopping bot..."
    if [ ! -z "$BOT_PID" ] && kill -0 "$BOT_PID" 2>/dev/null; then
        kill -TERM "$BOT_PID" 2>/dev/null
        wait "$BOT_PID" 2>/dev/null
    fi
    log "INFO" "Bot stopped gracefully"
    exit 0
}

trap cleanup SIGINT SIGTERM

# === MAIN LOOP ===
log "INFO" "=========================================="
log "INFO" "MONAD BOT Supervisor starting"
log "INFO" "Bot directory: ${BOT_DIR}"
log "INFO" "Log directory: ${LOG_DIR}"
log "INFO" "Max restarts: ${MAX_RESTARTS}"
log "INFO" "=========================================="

# Run backup before starting
if [ -f "${BOT_DIR}/backup.py" ]; then
    log "INFO" "Running backup..."
    "${VENV_PATH}/bin/python3" "${BOT_DIR}/backup.py" >> "${LOG_DIR}/supervisor.log" 2>&1
fi

while true; do
    # Check restart limit
    if [ $RESTART_COUNT -ge $MAX_RESTARTS ]; then
        log "ERROR" "Maximum restart limit (${MAX_RESTARTS}) reached. Exiting."
        exit 1
    fi

    # Generate daily log filename
    DATE_SUFFIX=$(date '+%Y%m%d')
    BOT_LOG="${LOG_DIR}/bot_${DATE_SUFFIX}.log"

    log "INFO" "Starting MONAD BOT (attempt $((RESTART_COUNT + 1))/${MAX_RESTARTS})"
    log "INFO" "Bot output will be logged to: ${BOT_LOG}"

    # Run the bot
    "${VENV_PATH}/bin/python3" "${BOT_DIR}/run_agents.py" >> "$BOT_LOG" 2>&1 &
    BOT_PID=$!
    
    log "INFO" "Bot started with PID: ${BOT_PID}"
    echo "$BOT_PID" > "${BOT_DIR}/bot.pid"

    # Wait for bot to exit
    wait $BOT_PID
    EXIT_CODE=$?
    
    rm -f "${BOT_DIR}/bot.pid"

    # Check exit code
    if [ $EXIT_CODE -eq 0 ]; then
        log "INFO" "Bot exited cleanly (exit code 0). No restart needed."
        exit 0
    fi

    log "WARN" "Bot crashed with exit code: ${EXIT_CODE}"
    RESTART_COUNT=$((RESTART_COUNT + 1))

    if [ $RESTART_COUNT -lt $MAX_RESTARTS ]; then
        log "INFO" "Waiting ${RESTART_DELAY}s before restart..."
        sleep $RESTART_DELAY
    fi
done
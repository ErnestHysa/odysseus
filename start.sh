#!/usr/bin/env bash
# Odysseus + SearXNG + ChromaDB startup script
# Usage: ./start.sh          (foreground, Ctrl+C to stop all)
#        ./start.sh stop     (stop background services)

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"

# ── Config ──
SEARXNG_PORT=8080
CHROMA_PORT=8100
ODYSSEUS_PORT=7001
SEARXNG_SETTINGS="$BASE_DIR/config/searxng/settings.yml"

# ── Stop command ──
if [[ "${1:-}" == "stop" ]]; then
    echo "Stopping Odysseus stack..."
    pkill -f "uvicorn app:app.*--port $ODYSSEUS_PORT" 2>/dev/null && echo "  Stopped Odysseus" || echo "  Odysseus not running"
    pkill -f "chroma run.*--port $CHROMA_PORT" 2>/dev/null && echo "  Stopped ChromaDB" || echo "  ChromaDB not running"
    pkill -f "searxng.webapp.*--port $SEARXNG_PORT" 2>/dev/null && echo "  Stopped SearXNG" || echo "  SearXNG not running"
    echo "Done."
    exit 0
fi

source venv/bin/activate

# ── 1. ChromaDB ──
if curl -sf "http://localhost:$CHROMA_PORT/api/v2/heartbeat" > /dev/null 2>&1; then
    echo "[ok] ChromaDB already running on :$CHROMA_PORT"
else
    echo "[..] Starting ChromaDB on :$CHROMA_PORT ..."
    chroma run --host localhost --port "$CHROMA_PORT" --path "$BASE_DIR/data/chroma" &>/dev/null &
    CHROMA_PID=$!
    # Wait for it
    for i in $(seq 1 15); do
        if curl -sf "http://localhost:$CHROMA_PORT/api/v2/heartbeat" > /dev/null 2>&1; then
            echo "[ok] ChromaDB ready (pid $CHROMA_PID)"
            break
        fi
        sleep 1
    done
    if ! curl -sf "http://localhost:$CHROMA_PORT/api/v2/heartbeat" > /dev/null 2>&1; then
        echo "[!!] ChromaDB failed to start. Check port $CHROMA_PORT."
        exit 1
    fi
fi

# ── 2. SearXNG ──
if curl -sf "http://localhost:$SEARXNG_PORT" > /dev/null 2>&1; then
    echo "[ok] SearXNG already running on :$SEARXNG_PORT"
else
    echo "[..] Starting SearXNG on :$SEARXNG_PORT ..."
    SEARXNG_SETTINGS_PATH="$SEARXNG_SETTINGS" \
    python -c "
from werkzeug.serving import run_simple
from searx.webapp import app
run_simple('127.0.0.1', $SEARXNG_PORT, app, use_reloader=False, threaded=True)
" &>/dev/null &
    SEARX_PID=$!
    for i in $(seq 1 15); do
        if curl -sf "http://localhost:$SEARXNG_PORT" > /dev/null 2>&1; then
            echo "[ok] SearXNG ready (pid $SEARX_PID)"
            break
        fi
        sleep 1
    done
    if ! curl -sf "http://localhost:$SEARXNG_PORT" > /dev/null 2>&1; then
        echo "[!!] SearXNG failed to start. Check port $SEARXNG_PORT."
        exit 1
    fi
fi

# ── 3. Odysseus ──
echo "[..] Starting Odysseus on :$ODYSSEUS_PORT ..."
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  Odysseus  http://localhost:$ODYSSEUS_PORT"
echo "║  SearXNG   http://localhost:$SEARXNG_PORT"
echo "║  ChromaDB  http://localhost:$CHROMA_PORT"
echo "║"
echo "║  Press Ctrl+C to stop all services"
echo "╚══════════════════════════════════════════════╝"
echo ""

# Trap Ctrl+C to kill all background processes
trap 'echo ""; echo "Stopping..."; kill $(jobs -p) 2>/dev/null; exit 0' SIGINT SIGTERM

uvicorn app:app --host 127.0.0.1 --port "$ODYSSEUS_PORT"

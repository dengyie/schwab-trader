#!/usr/bin/env bash
#
# Local dev launcher.
#
# Default is SIM mode: simulated quotes + in-process virtual matching, no
# Alpaca credentials, no upstream session. That default exists for a reason —
# a second process authenticating with the production Alpaca key evicts the
# VPS stream and the two then disconnect each other every few seconds (seen
# 2026-11-10: 86 disconnects in minutes, each "reconnecting in 1.0s").
#
# Use `./start.sh --real` only when you deliberately want this machine to hold
# the real Alpaca IEX session and trade the paper account. Stop the VPS
# service first or expect the stream to flap.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="sim"
WITH_FRONTEND=1
PROD_ENV="${PROD_ENV:-$HOME/.config/schwab-trader/prod.env}"

usage() {
    cat <<'EOF'
Usage: ./start.sh [--sim | --real] [--no-frontend]

  --sim           (default) simulated market data + virtual broker, no network
  --real          Alpaca IEX stream + Alpaca paper broker (needs credentials)
  --no-frontend   start the backend only (API on :8000)
EOF
}

for arg in "$@"; do
    case "$arg" in
        --sim) MODE="sim" ;;
        --real|--alpaca) MODE="real" ;;
        --no-frontend) WITH_FRONTEND=0 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

# --- Provider wiring -------------------------------------------------------
if [ "$MODE" = "real" ]; then
    if [ -f "$PROD_ENV" ]; then
        # Keys live outside the repo with 600 perms; never echo them.
        set -a
        # shellcheck disable=SC1090
        source "$PROD_ENV"
        set +a
    fi
    if [ -z "${ALPACA_API_KEY:-}" ] || [ -z "${ALPACA_API_SECRET:-}" ]; then
        echo "✖ --real needs ALPACA_API_KEY / ALPACA_API_SECRET (env or $PROD_ENV)." >&2
        echo "  Refusing to start: alpaca mode without keys would crash on import." >&2
        exit 1
    fi
    if [ -z "${ADMIN_TOKEN:-}" ]; then
        echo "✖ --real needs ADMIN_TOKEN as well (env or $PROD_ENV)." >&2
        exit 1
    fi
    export MARKET_DATA_PROVIDER=alpaca
    export BROKER_PROVIDER=alpaca_paper
    echo "⚠️  REAL MODE: this process will hold the Alpaca IEX session for this key."
    echo "⚠️  Production VPS uses the same key — stop it, or the two streams will evict each other."
else
    export MARKET_DATA_PROVIDER=simulation
    export BROKER_PROVIDER=virtual
    # Local-only token so the login gate works; there is no real account behind it.
    export ADMIN_TOKEN="${ADMIN_TOKEN:-local-dev-token}"
    echo "🧪 SIM MODE: synthetic quotes + virtual broker (no Alpaca session, no network)."
    echo "   Login token: $ADMIN_TOKEN"
fi

echo "🚀 Starting Schwab Trader Hub..."

# --- Backend ---------------------------------------------------------------
cd "$DIR/backend"
if [ ! -d ".venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt \
        || .venv/bin/pip install fastapi "uvicorn[standard]" pydantic pytest httpx websockets
fi

echo "Starting Backend on http://127.0.0.1:8000 (provider=$MARKET_DATA_PROVIDER broker=$BROKER_PROVIDER)..."
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!

if [ "$WITH_FRONTEND" = "0" ]; then
    trap 'echo "Stopping services..."; kill $BACKEND_PID 2>/dev/null || true; exit 0' INT TERM EXIT
    echo "=========================================================="
    echo "✨ Backend only — API: http://127.0.0.1:8000/docs"
    echo "=========================================================="
    wait $BACKEND_PID
    exit 0
fi

# --- Frontend --------------------------------------------------------------
cd "$DIR/frontend"
if [ ! -d "node_modules" ]; then
    echo "Installing frontend dependencies..."
    npm install
fi

echo "Starting Frontend on http://127.0.0.1:5173 ..."
npm run dev -- --host 127.0.0.1 --port 5173 &
FRONTEND_PID=$!

trap 'echo "Stopping services..."; kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true; exit 0' INT TERM EXIT

echo "=========================================================="
echo "✨ Schwab Trader Hub is Running!  (mode: $MODE)"
echo "📈 Web Dashboard: http://127.0.0.1:5173"
echo "🔌 Backend API:   http://127.0.0.1:8000/docs"
echo "⚡ Real-time WS:   ws://127.0.0.1:8000/ws"
echo "=========================================================="
echo "Press Ctrl+C to stop all services."

wait

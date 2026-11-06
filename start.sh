#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "🚀 Starting Schwab Trader Hub (Paper Trading & Data Hub)..."

# 1. Backend Startup
cd "$DIR/backend"
if [ ! -d ".venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt || pip install fastapi "uvicorn[standard]" pydantic pytest httpx websockets
else
    source .venv/bin/activate
fi

echo "Starting Backend on http://127.0.0.1:8000 ..."
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!

# 2. Frontend Startup
cd "$DIR/frontend"
if [ ! -d "node_modules" ]; then
    echo "Installing frontend dependencies..."
    npm install
fi

echo "Starting Frontend on http://127.0.0.1:5173 ..."
npm run dev -- --host 127.0.0.1 --port 5173 &
FRONTEND_PID=$!

trap "echo 'Stopping services...'; kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true; exit 0" INT TERM EXIT

echo "=========================================================="
echo "✨ Schwab Trader Hub is Running!"
echo "📈 Web Dashboard: http://127.0.0.1:5173"
echo "🔌 Backend API:   http://127.0.0.1:8000/docs"
echo "⚡ Real-time WS:   ws://127.0.0.1:8000/ws"
echo "=========================================================="
echo "Press Ctrl+C to stop all services."

wait

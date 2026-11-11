#!/bin/bash
set -e

# Ensure workspace directories exist in case a volume was mounted
mkdir -p /app/workspace/repositories /app/workspace/runs /app/workspace/venvs 2>/dev/null || true

export PORT="${PORT:-8000}"

if [ $# -gt 0 ]; then
  exec "$@"
fi

echo "Starting AgentHarness Lab backend on port ${PORT}..."
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --workers 1


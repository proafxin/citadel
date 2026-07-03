#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

if ! docker compose exec -T postgres pg_isready -U "${CITADEL_POSTGRES_USER:-postgres}" >/dev/null 2>&1; then
    echo "infra not up — run: bash scripts/infra.sh" >&2
    exit 1
fi

uv run alembic upgrade head

trap 'kill 0' EXIT

uv run fastapi dev citadel/app.py &

if [ ! -d ui/node_modules ]; then
    (cd ui && bun install)
fi
(cd ui && bun run dev) &

echo "app up — UI: http://localhost:5173   API: http://localhost:8000   (Ctrl+C stops app/worker/UI; infra stays up)"

uv run python citadel/worker.py

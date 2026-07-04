#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

if ! curl -sf "http://localhost:${CITADEL_MINERU_PORT:-8099}/v1/models" >/dev/null 2>&1; then
    echo "infra not up — run: bash scripts/infra.sh" >&2
    exit 1
fi

# fresh postgres each run: schema tracks code, so bring it down with its volumes and recreate.
docker compose down postgres --remove-orphans -v
docker compose up -d postgres

until docker compose exec -T postgres pg_isready -U "${CITADEL_POSTGRES_USER:-postgres}" >/dev/null 2>&1; do
    sleep 1
done
docker compose exec -T postgres psql -U "${CITADEL_POSTGRES_USER:-postgres}" -d "${CITADEL_POSTGRES_DB:-citadel}" -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null

uv run alembic upgrade head

trap 'kill 0' EXIT

uv run fastapi dev citadel/app.py &

if [ ! -d ui/node_modules ]; then
    (cd ui && bun install)
fi
(cd ui && bun run dev) &

echo "app up — UI: http://localhost:5173   API: http://localhost:8000   (fresh DB; Ctrl+C stops app/worker/UI)"

uv run python citadel/worker.py

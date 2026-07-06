#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

if ! curl -sf "http://localhost:${CITADEL_MINERU_PORT:-8099}/v1/models" >/dev/null 2>&1; then
    echo "infra not up — run: bash scripts/infra.sh" >&2
    exit 1
fi

# fresh state each run: wipe redis (streams reference doc ids from the wiped DB) and recreate postgres.
docker compose exec -T redis redis-cli FLUSHALL >/dev/null
docker compose down postgres --remove-orphans -v
docker compose up -d postgres

until docker compose exec -T postgres pg_isready -U "${CITADEL_POSTGRES_USER:-postgres}" >/dev/null 2>&1; do
    sleep 1
done
docker compose exec -T postgres psql -U "${CITADEL_POSTGRES_USER:-postgres}" -d "${CITADEL_POSTGRES_DB:-citadel}" -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null

uv run alembic upgrade head

if [ ! -d ui/node_modules ]; then
    (cd ui && bun install)
fi

# job control: each service runs in its OWN process group so shutdown can kill its whole tree
# (fastapi-dev's uvicorn worker, the worker's spawn process pools, vite's children) — not just the parent.
set -m

pids=()
uv run fastapi dev citadel/app.py & pids+=("$!")
(cd ui && bun run dev) & pids+=("$!")
uv run python citadel/worker.py & pids+=("$!")

cleanup() {
    trap - INT TERM EXIT
    for pid in "${pids[@]}"; do kill -s TERM -- -"$pid" 2>/dev/null || true; done
    sleep 2
    for pid in "${pids[@]}"; do kill -s KILL -- -"$pid" 2>/dev/null || true; done
}
trap cleanup INT TERM EXIT

echo "app up — UI: http://localhost:5173   API: http://localhost:8000   (fresh DB; Ctrl+C stops app/worker/UI)"

wait

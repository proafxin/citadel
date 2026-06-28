#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

docker compose up -d

until docker compose exec -T postgres pg_isready -U "${CITADEL_POSTGRES_USER:-postgres}" >/dev/null 2>&1; do
    sleep 1
done

until curl -sf "http://localhost:${CITADEL_MINERU_PORT:-8099}/v1/models" >/dev/null 2>&1; do
    sleep 2
done

uv run alembic upgrade head

trap 'kill 0' EXIT

uv run fastapi dev citadel/app.py &
uv run python citadel/worker.py

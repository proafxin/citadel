#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

docker compose up -d

until docker compose exec -T postgres pg_isready -U "${CITADEL_POSTGRES_USER:-postgres}" >/dev/null 2>&1; do
    sleep 1
done

uv run alembic upgrade head

trap 'kill 0' EXIT

uv run uvicorn citadel.app:app --host 0.0.0.0 --port 8000 &
uv run python citadel/worker.py

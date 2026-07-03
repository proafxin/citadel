#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

docker compose up -d

until docker compose exec -T postgres pg_isready -U "${CITADEL_POSTGRES_USER:-postgres}" >/dev/null 2>&1; do
    sleep 1
done

docker compose exec -T postgres psql -U "${CITADEL_POSTGRES_USER:-postgres}" -d "${CITADEL_POSTGRES_DB:-citadel}" -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null

until curl -sf "http://localhost:${CITADEL_MINERU_PORT:-8099}/v1/models" >/dev/null 2>&1 \
    && curl -sf "http://localhost:${CITADEL_QWEN_PORT:-8100}/v1/models" >/dev/null 2>&1; do
    for name in citadel-mineru citadel-qwen; do
        if [ "$(docker inspect -f '{{.RestartCount}}' "$name" 2>/dev/null || echo 99)" -ge 3 ]; then
            echo "ERROR: $name is crash-looping or missing — check: docker compose logs $name" >&2
            exit 1
        fi
    done
    sleep 2
done

echo "infra up — postgres, redis, mineru, qwen ready. now run: bash scripts/run.sh"

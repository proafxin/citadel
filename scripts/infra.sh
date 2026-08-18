#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

docker compose up -d qwen redis

until curl -sf "http://localhost:${CITADEL_QWEN_PORT:-8100}/v1/models" >/dev/null 2>&1; do
    if [ "$(docker inspect -f '{{.RestartCount}}' citadel-qwen 2>/dev/null || echo 99)" -ge 3 ]; then
        echo "ERROR: citadel-qwen is crash-looping or missing — check: docker compose logs citadel-qwen" >&2
        exit 1
    fi
    sleep 2
done

docker compose up -d bge

until curl -sf "http://localhost:${CITADEL_BGE_PORT:-8101}/v1/models" >/dev/null 2>&1; do
    if [ "$(docker inspect -f '{{.RestartCount}}' citadel-bge 2>/dev/null || echo 99)" -ge 3 ]; then
        echo "ERROR: citadel-bge is crash-looping or missing — check: docker compose logs citadel-bge" >&2
        exit 1
    fi
    sleep 2
done

echo "infra up — qwen, bge, redis ready. now run: bash scripts/run.sh"

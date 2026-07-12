#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

# heavy, long-lived services (model load time) — bring up once and leave running.
# postgres is intentionally NOT here: run.sh recreates it each run because its schema tracks code.
docker compose up -d paddleocr qwen redis

until curl -sf "http://localhost:${CITADEL_PADDLEOCR_PORT:-8099}/v1/models" >/dev/null 2>&1 \
    && curl -sf "http://localhost:${CITADEL_QWEN_PORT:-8100}/v1/models" >/dev/null 2>&1; do
    for name in citadel-paddleocr citadel-qwen; do
        if [ "$(docker inspect -f '{{.RestartCount}}' "$name" 2>/dev/null || echo 99)" -ge 3 ]; then
            echo "ERROR: $name is crash-looping or missing — check: docker compose logs $name" >&2
            exit 1
        fi
    done
    sleep 2
done

echo "infra up — paddleocr, qwen, redis ready. now run: bash scripts/run.sh"

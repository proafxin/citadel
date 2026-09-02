#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

docker compose up -d lm redis

until curl -sf "http://localhost:${CITADEL_LM_PORT:-8100}/v1/models" >/dev/null 2>&1; do
    if [ "$(docker inspect -f '{{.RestartCount}}' lm 2>/dev/null || echo 99)" -ge 3 ]; then
        echo "ERROR: lm is crash-looping or missing — check: docker compose logs lm" >&2
        exit 1
    fi
    sleep 2
done

echo "infra up — lm, redis ready (embedding disabled). now run: bash scripts/run.sh"

#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

# paddle (the layout detector) PRE-ALLOCATES ~92% of the GPU the instant it initialises, whatever else is on the
# card — we watched it grab the lot and then die allocating 144MB. it shares this GPU with the recognition model,
# the SLM and the embedder, so make it grow on demand instead. must be set before python imports paddle.
export FLAGS_allocator_strategy=auto_growth
export FLAGS_fraction_of_gpu_memory_to_use=0

# a pool worker churns big transient buffers — a rendered page is a ~19MB bitmap plus a PIL copy plus a PNG — and glibc
# turns that into a ratchet it never releases: measured 219MB of [heap] in a worker whose live data is a few MB.
# MMAP_THRESHOLD_ is the one that matters. glibc mmaps blocks over the threshold and munmaps them on free, but on each
# such free it RAISES the threshold to that block's size (up to 32MB), assuming the size will recur. after a few pages
# the threshold clears 19MB, every bitmap is served from the heap instead, and the heap grows and fragments forever.
# setting it explicitly pins it and disables that adaptation, so large buffers go back to being mmap'd and returned to
# the OS the moment they are freed. ARENA_MAX caps the per-thread arenas glibc would otherwise hold alongside it.
# inherited by the forkserver (which is exec'd) and so by every pdfium and header pool child.
export MALLOC_MMAP_THRESHOLD_=131072
export MALLOC_ARENA_MAX=2
export MALLOC_TRIM_THRESHOLD_=131072

docker compose exec -T redis redis-cli FLUSHALL >/dev/null
docker compose down postgres --remove-orphans
docker compose up -d postgres

if ! curl -sf "http://localhost:${CITADEL_PADDLEOCR_PORT:-8099}/v1/models" >/dev/null 2>&1; then
    echo "infra not up — run: bash scripts/infra.sh" >&2
    exit 1
fi

# fresh state each run: wipe redis (streams reference doc ids from the wiped DB) and recreate postgres.

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
uv run fastapi run citadel/app.py & pids+=("$!")
(cd ui && bun run dev) & pids+=("$!")
uv run python citadel/worker.py & pids+=("$!")
uv run python citadel/slm.py & pids+=("$!")

cleanup() {
    trap - INT TERM EXIT
    for pid in "${pids[@]}"; do kill -s TERM -- -"$pid" 2>/dev/null || true; done
    sleep 2
    for pid in "${pids[@]}"; do kill -s KILL -- -"$pid" 2>/dev/null || true; done
}
trap cleanup INT TERM EXIT

echo "app up — UI: http://localhost:5173   API: http://localhost:8000   (fresh DB; Ctrl+C stops app/worker/UI)"

wait

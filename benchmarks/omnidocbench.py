# OmniDocBench eval harness for citadel. Run the stages in order:
#
#   1) get the data — images + the GT json (the imagefolder `load_dataset` loader drops the json, so pull the
#      whole dataset snapshot; ~1.6k pages, a couple GB):
#        uv run python -c "from huggingface_hub import snapshot_download; \
#          snapshot_download('opendatalab/OmniDocBench', repo_type='dataset', local_dir='data/omnidocbench')"
#
#   2) predict — with the citadel stack up (docker compose up -d + the worker running), ingest each eval image
#      DIRECTLY (citadel takes images natively → scanned OCR path) and write one markdown prediction per page
#      named <image_stem>.md — the format OmniDocBench's end2end scorer expects (tables as HTML, formulas $$..$$):
#        uv run python benchmarks/omnidocbench.py data/omnidocbench
#      (also prints a real pages/sec over the full ~1.6k-page set — the throughput number for the pitch.)
#
#   3) score with OmniDocBench's OFFICIAL scorer. It needs python <3.12 (ours is 3.13) → separate env:
#        git clone https://github.com/opendatalab/OmniDocBench && cd OmniDocBench
#        uv venv --python 3.11 && uv pip install -e .
#        # in configs/end2end.yaml set:
#        #   end2end_eval.dataset.ground_truth.data_path -> <abs>/data/omnidocbench/OmniDocBench.json
#        #   end2end_eval.dataset.prediction.data_path   -> <abs>/benchmarks/out/omnidocbench/pred
#        # and drop the formula "- CDM" metric line unless TeX Live + ImageMagick + Ghostscript are set up
#        # (Text^Edit, Table^TEDS, Reading-Order, and formula Edit_dist all work on the pip deps alone).
#        uv run python pdf_validation.py --config configs/end2end.yaml
import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

logger = logging.getLogger("omnidocbench")

POLL_INTERVAL = 1.0  # seconds between /result polls — gentle on the app (128 concurrent docs polling adds up)

PRED = Path("benchmarks/out/omnidocbench/pred")  # <image_stem>.md predictions the scorer aligns by filename

# categories OmniDocBench does not score as page text → keep them out of the prediction markdown so they
# don't add unmatched noise to the text edit distance (figures carry no text GT; page numbers are pure noise)
SKIP_TYPES = {"abandon", "page_number", "image", "error", "header", "footer"}


def _image_file(root: Path, image_path: str) -> Path:
    name = Path(image_path).name
    nested = root / "images" / name
    return nested if nested.exists() else root / image_path


def render_md(blocks: list[dict]) -> str:
    parts: list[str] = []
    for block in blocks:
        kind = block.get("type", "text")
        text = (block.get("text") or "").strip()
        if kind in SKIP_TYPES:
            continue
        if kind == "table" and text:
            parts.append(text)  # block.text is the <table> HTML → scored by TEDS
        elif kind == "equation" and text:
            parts.append(f"$$\n{text}\n$$")  # display formula → CDM / latex edit distance
        elif kind == "title" and text:
            parts.append(f"# {text}")
        elif kind == "list" and block.get("list_items"):
            parts.extend(f"- {item}" for item in block["list_items"])
        elif text:
            parts.append(text)
    return "\n\n".join(parts)


async def _predict_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str, img: Path) -> None:
    async with sem:  # bound docs in flight so we feed the pipeline without flooding it with 1.6k pending docs
        resp = await client.post(f"{url}/ingest", files={"files": (img.name, img.read_bytes())})
        resp.raise_for_status()
        doc_id = resp.json()["doc_ids"][0]
        while True:
            result = await client.get(f"{url}/result/{doc_id}")
            if result.status_code == httpx.codes.OK:
                break
            await asyncio.sleep(POLL_INTERVAL)
        (PRED / f"{img.stem}.md").write_text(render_md(result.json()["blocks"]))


async def run_predict(dataset_dir: str, url: str, concurrency: int) -> None:
    root = Path(dataset_dir)
    pages = json.loads((root / "OmniDocBench.json").read_text())
    images = [_image_file(root, page["page_info"]["image_path"]) for page in pages]
    PRED.mkdir(parents=True, exist_ok=True)
    todo = [img for img in images if not (PRED / f"{img.stem}.md").exists()]  # resume: skip pages already written
    logger.info("%d pages total, %d already done, %d to do", len(images), len(images) - len(todo), len(todo))
    sem = asyncio.Semaphore(concurrency)
    # no keep-alive: each poll uses a fresh connection so we never reuse one uvicorn already closed
    # (that race surfaces as RemoteProtocolError "server disconnected"); cheap at benchmark scale
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(None), limits=httpx.Limits(max_keepalive_connections=0)
    ) as client:
        start = time.monotonic()
        # no return_exceptions: the first real failure propagates and crashes loudly with its stacktrace
        await asyncio.gather(*(_predict_one(client, sem, url, img) for img in todo))
        elapsed = time.monotonic() - start
    logger.info("predicted %d pages in %.1fs → %.2f pages/s", len(todo), elapsed, len(todo) / max(elapsed, 1e-9))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # silence per-poll "GET /result ... 404" request logs
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset_dir")  # the snapshot dir: OmniDocBench.json + images/
    parser.add_argument("--url", default="http://localhost:8000")
    # the GPU handles high page concurrency fine; the real cost on an all-scanned set is the CPU RapidOCR gap-fill,
    # so run the worker with CITADEL_GAP_FILL=0 for this benchmark (pure VLM = the apples-to-apples MinerU match)
    parser.add_argument("--concurrency", type=int, default=128)
    args = parser.parse_args()
    asyncio.run(run_predict(args.dataset_dir, args.url, args.concurrency))


if __name__ == "__main__":
    main()

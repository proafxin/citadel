import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

logger = logging.getLogger("omnidocbench")

POLL_INTERVAL = 1.0

PRED = Path("benchmarks/out/omnidocbench/pred")

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
            parts.append(text)
        elif kind == "equation" and text:
            parts.append(f"$$\n{text}\n$$")
        elif kind == "title" and text:
            parts.append(f"# {text}")
        elif kind == "list" and block.get("list_items"):
            parts.extend(f"- {item}" for item in block["list_items"])
        elif text:
            parts.append(text)
    return "\n\n".join(parts)


async def _predict_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str, img: Path) -> None:
    async with sem:
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
    todo = [img for img in images if not (PRED / f"{img.stem}.md").exists()]
    logger.info("%d pages total, %d already done, %d to do", len(images), len(images) - len(todo), len(todo))
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(None), limits=httpx.Limits(max_keepalive_connections=0)
    ) as client:
        start = time.monotonic()
        await asyncio.gather(*(_predict_one(client, sem, url, img) for img in todo))
        elapsed = time.monotonic() - start
    logger.info("predicted %d pages in %.1fs → %.2f pages/s", len(todo), elapsed, len(todo) / max(elapsed, 1e-9))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset_dir")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--concurrency", type=int, default=128)
    args = parser.parse_args()
    asyncio.run(run_predict(args.dataset_dir, args.url, args.concurrency))


if __name__ == "__main__":
    main()

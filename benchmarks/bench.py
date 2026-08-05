import argparse
import asyncio
import difflib
import json
import logging
import re
import time
from pathlib import Path

import httpx

from citadel.schemas.tree import DocumentTree
from citadel.services import tree

logger = logging.getLogger("bench")
OUT = Path("benchmarks/out")


async def _ingest_one(client: httpx.AsyncClient, url: str, path: Path) -> tuple[str, str]:
    resp = await client.post(f"{url}/ingest", files={"files": (path.name, path.read_bytes())})
    resp.raise_for_status()
    return resp.json()["doc_ids"][0], path.name


async def _wait_result(client: httpx.AsyncClient, url: str, doc_id: str) -> dict:
    while True:
        resp = await client.get(f"{url}/result/{doc_id}")
        if resp.status_code == httpx.codes.OK:
            return resp.json()
        await asyncio.sleep(0.25)


async def run_citadel(docs_dir: str, url: str) -> None:
    docs = sorted(p for p in Path(docs_dir).iterdir() if p.is_file())
    out = OUT / "citadel"
    out.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=httpx.Timeout(None)) as client:
        start = time.monotonic()
        submitted = await asyncio.gather(*(_ingest_one(client, url, p) for p in docs))
        results = await asyncio.gather(*(_wait_result(client, url, doc_id) for doc_id, _ in submitted))
        elapsed = time.monotonic() - start
    for (_doc_id, name), result in zip(submitted, results, strict=True):
        (out / f"{name}.json").write_text(json.dumps(result))
    logger.info("citadel: %d docs in %.1fs → %.2f docs/s", len(docs), elapsed, len(docs) / elapsed)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _key(name: str) -> str:
    return Path(name).stem.lower()


def compare(citadel_dir: str, mineru_dir: str) -> None:
    citadel = {
        _key(p.stem): tree.render_markdown(DocumentTree.model_validate_json(p.read_text()))
        for p in Path(citadel_dir).glob("*.json")
    }
    mineru = {_key(p.stem): p.read_text(errors="replace") for p in Path(mineru_dir).rglob("*.md")}
    sims: list[float] = []
    for name in sorted(citadel):
        other = mineru.get(name)
        if other is None:
            logger.info("%-45s  (no mineru output)", name)
            continue
        sim = difflib.SequenceMatcher(None, _norm(citadel[name]), _norm(other)).ratio()
        sims.append(sim)
        logger.info("%-45s  %.3f", name, sim)
    if sims:
        logger.info("mean text similarity (citadel vs mineru): %.3f", sum(sims) / len(sims))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    cit = sub.add_parser("citadel")
    cit.add_argument("docs_dir")
    cit.add_argument("--url", default="http://localhost:8000")
    cmp = sub.add_parser("compare")
    cmp.add_argument("--citadel", default=str(OUT / "citadel"))
    cmp.add_argument("--mineru", default=str(OUT / "mineru"))
    args = parser.parse_args()
    if args.cmd == "citadel":
        asyncio.run(run_citadel(args.docs_dir, args.url))
    else:
        compare(args.citadel, args.mineru)


if __name__ == "__main__":
    main()

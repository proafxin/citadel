import asyncio
import io
import json
import logging
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

import filetype
import pypdfium2 as pdfium
import redis.asyncio as aioredis
from markdownify import markdownify
from redis.exceptions import ResponseError
from mineru_vl_utils import MinerUClient
from PIL import Image

from citadel.schemas.document import Block, ParsedDocument
from config import get_settings

logger = logging.getLogger(__name__)

GROUP = "citadel"
STREAM_INGEST = "ingest"
STREAM_NORMALIZED = "normalized"
STREAM_PAGES = "pages"
STREAM_MERGE = "merge"
STREAM_TABLES = "tables"

OFFICE_EXTS = {"doc", "docx", "ppt", "pptx", "odt", "odp", "rtf"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"}
TEXT_EXTS = {"txt", "md", "markdown"}
HTML_EXTS = {"html", "htm"}

MAX_ATTEMPTS = 3

@lru_cache
def get_redis() -> aioredis.Redis:
    # socket_timeout=None so the blocking XREADGROUP isn't cut off by a client read timeout
    return aioredis.from_url(get_settings().redis_url, socket_timeout=None)


@lru_cache
def get_mineru_client() -> MinerUClient:
    return MinerUClient(backend="http-client", server_url=get_settings().mineru_base_url)


def _raw_dir() -> Path:
    return get_settings().data_dir / "raw"


def _norm_dir() -> Path:
    return get_settings().data_dir / "normalized"


def _result_dir() -> Path:
    return get_settings().data_dir / "results"


# ---- orchestrator side ----------------------------------------------------------------


async def submit_document(data: bytes, filename: str) -> str:
    doc_id = uuid.uuid4().hex
    _raw_dir().mkdir(parents=True, exist_ok=True)
    (_raw_dir() / doc_id).write_bytes(data)
    redis = get_redis()
    await redis.hset(
        f"doc:{doc_id}",
        mapping={"state": "queued", "filename": filename, "done_count": 0, "t0": time.time()},
    )
    await redis.xadd(STREAM_INGEST, {"doc_id": doc_id, "filename": filename})
    logger.info("ingest file=%s", filename)
    return doc_id


async def get_status(doc_id: str) -> dict[str, str]:
    raw = await get_redis().hgetall(f"doc:{doc_id}")
    return {k.decode(): v.decode() for k, v in raw.items()}


async def get_result(doc_id: str) -> dict | None:
    path = _result_dir() / f"{doc_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


# ---- normalize stage (CPU / process pool) ---------------------------------------------


def _detect_ext(path: Path, filename: str) -> str:
    guess = filetype.guess(str(path))
    if guess is not None:
        return guess.extension
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def normalize_file(raw_path_str: str, doc_id: str, filename: str) -> str:
    raw_path = Path(raw_path_str)
    ext = _detect_ext(raw_path, filename)
    _norm_dir().mkdir(parents=True, exist_ok=True)
    if ext in OFFICE_EXTS:
        soffice = shutil.which("soffice")
        if soffice is None:
            raise RuntimeError("libreoffice 'soffice' not found on PATH")
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(_norm_dir()), str(raw_path)],
            check=True,
            capture_output=True,
        )
        produced = _norm_dir() / f"{raw_path.stem}.pdf"
        target = _norm_dir() / f"{doc_id}.pdf"
        produced.rename(target)
        return "pdf"
    if ext == "pdf":
        shutil.copyfile(raw_path, _norm_dir() / f"{doc_id}.pdf")
        return "pdf"
    if ext in IMAGE_EXTS:
        shutil.copyfile(raw_path, _norm_dir() / f"{doc_id}.{ext}")
        return f"image:{ext}"
    if ext in HTML_EXTS:
        (_norm_dir() / f"{doc_id}.md").write_text(markdownify(raw_path.read_text()))
        return "text"
    # txt / md / unknown → treat as text
    (_norm_dir() / f"{doc_id}.md").write_text(raw_path.read_text(errors="replace"))
    return "text"


async def handle_normalize(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    await get_redis().hset(f"doc:{doc_id}", "state", "normalizing")
    kind = await asyncio.to_thread(normalize_file, str(_raw_dir() / doc_id), doc_id, fields["filename"])
    await get_redis().xadd(STREAM_NORMALIZED, {"doc_id": doc_id, "kind": kind, "filename": fields["filename"]})
    logger.info("normalize file=%s kind=%s", fields["filename"], kind)


# ---- paginate stage (CPU / process pool) ----------------------------------------------


def render_pdf_pages(pdf_path_str: str, dpi: int) -> list[bytes]:
    images: list[bytes] = []
    pdf = pdfium.PdfDocument(pdf_path_str)
    scale = dpi / 72
    for page in pdf:
        pil = page.render(scale=scale).to_pil()
        bio = io.BytesIO()
        pil.save(bio, format="PNG")
        images.append(bio.getvalue())
    return images


async def handle_paginate(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    kind = fields["kind"]
    redis = get_redis()
    await redis.hset(f"doc:{doc_id}", "state", "paginating")
    if kind == "text":
        md = (_norm_dir() / f"{doc_id}.md").read_text()
        await redis.hset(f"doc:{doc_id}", "page_count", 1)
        await record_page(doc_id, 0, [Block(type="text", page_idx=0, text=md)])
        logger.info("paginate file=%s text", fields["filename"])
        return
    if kind.startswith("image:"):
        image_bytes = (_norm_dir() / f"{doc_id}.{kind.split(':', 1)[1]}").read_bytes()
        await redis.hset(f"doc:{doc_id}", "page_count", 1)
        await redis.xadd(STREAM_PAGES, {"doc_id": doc_id, "page_idx": 0, "image": image_bytes})
        logger.info("paginate file=%s image", fields["filename"])
        return
    pages = await asyncio.to_thread(render_pdf_pages, str(_norm_dir() / f"{doc_id}.pdf"), get_settings().render_dpi)
    await redis.hset(f"doc:{doc_id}", "page_count", len(pages))
    for idx, image_bytes in enumerate(pages):
        await redis.xadd(STREAM_PAGES, {"doc_id": doc_id, "page_idx": idx, "image": image_bytes})
    logger.info("paginate file=%s pages=%d", fields["filename"], len(pages))


# ---- ocr stage (async I/O → vLLM via mineru-vl-utils) ----------------------------------


def map_content_block(block: object, page_idx: int) -> Block:
    # PROVISIONAL: confirm ContentBlock attribute names against the smoke-test output and adjust.
    get = (lambda k, d=None: getattr(block, k, d)) if not isinstance(block, dict) else block.get
    text = get("content") or get("text") or get("md") or ""
    return Block(
        type=str(get("type") or "text"),
        page_idx=page_idx,
        bbox=list(get("bbox") or []),
        text=str(text),
        list_items=list(get("list_items") or []),
    )


async def record_page(doc_id: str, page_idx: int, blocks: list[Block]) -> None:
    redis = get_redis()
    await redis.hset(f"blocks:{doc_id}", str(page_idx), json.dumps([asdict(b) for b in blocks]))
    done = await redis.hincrby(f"doc:{doc_id}", "done_count", 1)
    expected = int(await redis.hget(f"doc:{doc_id}", "page_count") or 0)
    if expected and done >= expected:
        await redis.xadd(STREAM_MERGE, {"doc_id": doc_id})


async def fail_page(doc_id: str, page_idx: int) -> None:
    # one page exhausting retries must not fail the whole doc: record a marker, keep going
    await record_page(doc_id, page_idx, [Block(type="error", page_idx=page_idx, text="[extraction failed]")])


async def fail_document(doc_id: str, stage: str) -> None:
    await get_redis().hset(f"doc:{doc_id}", mapping={"state": "failed", "error": f"{stage} failed"})


async def handle_ocr(fields: dict[str, str], image: bytes) -> None:
    doc_id = fields["doc_id"]
    page_idx = int(fields["page_idx"])
    content_blocks = await get_mineru_client().aio_two_step_extract(Image.open(io.BytesIO(image)))
    blocks = [map_content_block(cb, page_idx) for cb in content_blocks]
    for block in blocks:
        if block.type == "table":
            await get_redis().xadd(STREAM_TABLES, {"doc_id": doc_id, "page_idx": page_idx, "html": block.text})
    await record_page(doc_id, page_idx, blocks)


# ---- merge stage ----------------------------------------------------------------------


def blocks_to_markdown(blocks: list[Block]) -> str:
    lines: list[str] = []
    for block in blocks:
        if block.type in {"title", "header"} and block.text:
            lines.append(f"{'#' * (block.text_level or 1)} {block.text}")
        elif block.type == "list" and block.list_items:
            lines.extend(f"- {item}" for item in block.list_items)
        elif block.text:
            lines.append(block.text)
    return "\n\n".join(lines)


async def handle_merge(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    redis = get_redis()
    per_page = await redis.hgetall(f"blocks:{doc_id}")
    blocks: list[Block] = []
    for page_idx in sorted(int(k) for k in per_page):
        for raw in json.loads(per_page[str(page_idx).encode()]):
            blocks.append(Block(**raw))
    source = (await redis.hget(f"doc:{doc_id}", "filename") or b"").decode()
    document = ParsedDocument(source=source, markdown=blocks_to_markdown(blocks), blocks=blocks)
    _result_dir().mkdir(parents=True, exist_ok=True)
    (_result_dir() / f"{doc_id}.json").write_text(
        json.dumps({"source": document.source, "markdown": document.markdown, "blocks": [asdict(b) for b in blocks]})
    )
    state = "partial" if any(b.type == "error" for b in blocks) else "done"
    await redis.hset(f"doc:{doc_id}", "state", state)
    t0 = float(await redis.hget(f"doc:{doc_id}", "t0") or 0)
    logger.info("merge file=%s state=%s blocks=%d dur=%.1fs", source, state, len(blocks), time.time() - t0)
    await cleanup(doc_id)


async def cleanup(doc_id: str) -> None:
    redis = get_redis()
    await redis.delete(f"blocks:{doc_id}")
    await redis.expire(f"doc:{doc_id}", 3600)  # keep final status briefly, then auto-evict — no accumulation
    (_raw_dir() / doc_id).unlink(missing_ok=True)
    for path in _norm_dir().glob(f"{doc_id}.*"):
        path.unlink(missing_ok=True)


# ---- stage wiring (used by the worker entrypoint) -------------------------------------

STREAMS = (STREAM_INGEST, STREAM_NORMALIZED, STREAM_PAGES, STREAM_MERGE, STREAM_TABLES)


async def ensure_group(stream: str) -> None:
    try:
        await get_redis().xgroup_create(stream, GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise

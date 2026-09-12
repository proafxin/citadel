import asyncio
import ctypes
import gc
import io
import json
import logging
import multiprocessing
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import TypeVar

from cachetools import LRUCache
from fastapi import HTTPException, UploadFile
from PIL import Image, ImageStat
from redis.commands.core import AsyncScript
from redis.exceptions import ResponseError
from sqlalchemy import select

from citadel.bus import get_redis
from citadel.db import get_sessionmaker
from citadel.llm import call_page_ocr
from citadel.models.document import Document
from citadel.models.status import DocumentStatus
from citadel.schemas.content import Block
from citadel.schemas.document import DocProgress, DocumentRead, IngestResponse
from citadel.services.capacity import get_capacity, get_vision_capacity
from citadel.services.document import (
    begin_library_ingest,
    collect_tables,
    create_documents,
    dump_structures,
    dump_tables,
    finalize_tabular,
    load_structures,
    mark_document,
    mark_processing,
    persist_document_tree,
    prepare_document,
    save_document_tree,
    save_sheet_tables,
    table_block_indices,
)
from citadel.services.html import parse_html
from citadel.services.library import library_exists
from citadel.services.pdf import MAX_IMAGE_SIDE, count_pdf_pages, downscale, render_pdf_page, to_png_bytes
from citadel.services.presentation import parse_pptx
from citadel.services.tabular import (
    extract_json_tables,
    grid_from_markdown,
    single_table_structure,
    structure_csv_tables,
)
from citadel.tabular.materialize import MaterializedTable, materialize
from citadel.tabular.structure import structure_candidate
from citadel.utils import normalize_file
from config import CPU_EIGHTH, CPU_THIRD

logger = logging.getLogger(__name__)

GROUP = "citadel"
STREAM_INGEST = "ingest"
STREAM_NORMALIZED = "normalized"
RENDER_DOCS = "render:docs"
STREAM_RASTERIZE = "rasterize"
STREAM_PAGES = "pages"
STREAM_STRUCTURE = "structure"
STREAM_TABLE_STRUCTURE = "table_structure"
STREAM_MERGE = "merge"


def render_stream(doc_id: str) -> str:
    return f"render:{doc_id}"


MAX_ATTEMPTS = 3
DOC_TTL = 86_400
RENDER_DPI = 300
PAGINATE_CONCURRENCY = CPU_THIRD
RENDER_CONCURRENCY = CPU_EIGHTH
PDFIUM_WORKERS = 4
RASTERIZE_PULL_CONCURRENCY = PDFIUM_WORKERS
RENDER_TIMEOUT = 120
BULK_READ_COUNT = 256


T = TypeVar("T")
_PROCESS_POOLS: list[ProcessPoolExecutor] = []


@lru_cache
def get_pdfium_pool() -> ProcessPoolExecutor:
    ctx = multiprocessing.get_context("forkserver")
    ctx.set_forkserver_preload(["citadel.services.pdf"])
    pool = ProcessPoolExecutor(max_workers=PDFIUM_WORKERS, mp_context=ctx)
    _PROCESS_POOLS.append(pool)
    return pool


def _kill_pdfium_pool(pool: ProcessPoolExecutor) -> None:
    for process in pool._processes.values():
        process.kill()
    pool.shutdown(wait=False, cancel_futures=True)


async def _run_pdfium[T](func: Callable[..., T], *args: object, job_timeout: float) -> tuple[T, float, float]:
    mark = time.time()
    cap = get_capacity("pdfium", PDFIUM_WORKERS)
    await cap.acquire()
    try:
        wait = time.time() - mark
        mark = time.time()
        pool = get_pdfium_pool()
        future = pool.submit(func, *args)
        done, pending = await asyncio.wait({asyncio.wrap_future(future)}, timeout=job_timeout)
        if pending:
            logger.error("pdfium job exceeded %ds args=%r — killing pool", job_timeout, args)
            get_pdfium_pool.cache_clear()
            if _PROCESS_POOLS and _PROCESS_POOLS[-1] is pool:
                _PROCESS_POOLS.pop()
            _kill_pdfium_pool(pool)
            msg = f"pdfium job exceeded {job_timeout}s args={args!r}"
            raise TimeoutError(msg)
        result = next(iter(done)).result()
        return result, wait, time.time() - mark
    finally:
        await cap.release()


_PROFILE_DIRS: list[str] = []


def make_profile_pool(n: int) -> asyncio.Queue[str]:
    queue: asyncio.Queue[str] = asyncio.Queue()
    for _ in range(n):
        path = tempfile.mkdtemp(prefix="lo_profile_")
        _PROFILE_DIRS.append(path)
        queue.put_nowait(path)
    return queue


def _reap_profile_dirs() -> None:
    while _PROFILE_DIRS:
        path = Path(_PROFILE_DIRS.pop())
        if path.exists():
            shutil.rmtree(path)


def release_idle() -> None:
    while _PROCESS_POOLS:
        pool = _PROCESS_POOLS.pop()
        pool.shutdown(wait=True, cancel_futures=True)
    get_pdfium_pool.cache_clear()
    gc.collect()
    ctypes.CDLL("libc.so.6").malloc_trim(0)


async def shutdown() -> None:
    while _PROCESS_POOLS:
        pool = _PROCESS_POOLS.pop()
        pool.shutdown(wait=True, cancel_futures=True)
    _reap_profile_dirs()
    if get_redis.cache_info().currsize:
        await get_redis().aclose()


BLOB_DIR = Path(tempfile.gettempdir()) / "citadel-blobs"


async def _add_stage_seconds(doc_id: str, field: str, seconds: float) -> None:
    await get_redis().hincrbyfloat(f"doc:{doc_id}", field, seconds)


def blob_path(doc_id: str | int) -> Path:
    return BLOB_DIR / str(doc_id)


def page_image_key(doc_id: str, page_idx: int) -> str:
    return f"page_image:{doc_id}:{page_idx}"


async def reap_orphan_blobs() -> None:
    BLOB_DIR.mkdir(parents=True, exist_ok=True)
    async with get_sessionmaker()() as session:
        active = set(
            await session.scalars(
                select(Document.id).where(Document.status.in_((DocumentStatus.QUEUED, DocumentStatus.PROCESSING)))
            )
        )
    for path in BLOB_DIR.iterdir():
        if not (path.name.isdigit() and int(path.name) in active):
            path.unlink(missing_ok=True)


async def _doc_terminal(doc_id: str) -> bool:
    async with get_sessionmaker()() as session:
        status = await session.scalar(select(Document.status).where(Document.id == int(doc_id)))
    return status not in {DocumentStatus.QUEUED, DocumentStatus.PROCESSING}


async def submit_documents(files: list[UploadFile], library_id: int) -> IngestResponse:
    if not await library_exists(library_id):
        raise HTTPException(status_code=404, detail="unknown library")
    payloads = [(await file.read(), file.filename or "upload") for file in files]
    doc_ids = await create_documents(library_id, [name for _data, name in payloads])
    await begin_library_ingest(library_id)
    now = time.time()
    BLOB_DIR.mkdir(parents=True, exist_ok=True)
    pipe = get_redis().pipeline(transaction=False)
    for doc_id, (data, name) in zip(doc_ids, payloads, strict=True):
        await asyncio.to_thread(blob_path(doc_id).write_bytes, data)
        pipe.hset(
            f"doc:{doc_id}",
            mapping={"state": "queued", "filename": name, "library_id": library_id, "done_count": 0, "t0": now},
        )
        pipe.expire(f"doc:{doc_id}", DOC_TTL)
        pipe.xadd(STREAM_INGEST, {"doc_id": doc_id, "filename": name})
    await pipe.execute()
    logger.info("ingest library=%s files=%d doc_ids=%s", library_id, len(files), doc_ids)
    return IngestResponse(doc_ids=doc_ids)


async def list_documents(library_id: int) -> list[DocumentRead]:
    if not await library_exists(library_id):
        raise HTTPException(status_code=404, detail="unknown library")
    async with get_sessionmaker()() as session:
        rows = list(
            await session.execute(
                select(Document.id, Document.filename, Document.status, Document.ingest_seconds)
                .where(Document.library_id == library_id)
                .order_by(Document.id)
            )
        )
    return [
        DocumentRead(id=doc_id, filename=filename, status=status, elapsed=ingest_seconds)
        for doc_id, filename, status, ingest_seconds in rows
    ]


def _progress_status(state: bytes | None, started: int, done: int) -> str:
    internal = state.decode() if state else ""
    if internal == "failed":
        return "Failed"
    if internal == "ingested":
        return "Ready"
    if started > 0 or done > 0:
        return "Processing"
    if internal == "paginating":
        return "Waiting"
    return "Queued"


async def library_progress(library_id: int) -> list[DocProgress]:
    async with get_sessionmaker()() as session:
        rows = list(
            await session.execute(
                select(Document.id, Document.filename)
                .where(Document.library_id == library_id, Document.status == DocumentStatus.PROCESSING)
                .order_by(Document.id)
            )
        )
    redis = get_redis()
    progress: list[DocProgress] = []
    for doc_id, filename in rows:
        fields = await redis.hmget(f"doc:{doc_id}", "done_count", "started_count", "page_count", "state")
        done, started, total = (int(value) if value else 0 for value in fields[:3])
        progress.append(
            DocProgress(
                doc_id=doc_id,
                filename=filename,
                status=_progress_status(fields[3], started, done),
                done=done,
                active=max(started - done, 0),
                total=total,
            )
        )
    return progress


def normalize_media_key(doc_id: str) -> str:
    return f"normalize_media:{doc_id}"


async def handle_normalize(fields: dict[str, str], profile_dir: str) -> None:
    doc_id = fields["doc_id"]
    redis = get_redis()
    await redis.hset(f"doc:{doc_id}", "state", "normalizing")
    await redis.hsetnx(f"doc:{doc_id}", "t_proc", time.time())
    await mark_processing(int(doc_id))
    data = await asyncio.to_thread(blob_path(doc_id).read_bytes)
    kind, normalized, media = await asyncio.to_thread(normalize_file, data, fields["filename"], profile_dir)
    await asyncio.to_thread(blob_path(doc_id).write_bytes, normalized)
    if media:
        await redis.hset(normalize_media_key(doc_id), mapping=media)
        await redis.expire(normalize_media_key(doc_id), DOC_TTL)
    await get_redis().xadd(STREAM_NORMALIZED, {"doc_id": doc_id, "kind": kind, "filename": fields["filename"]})


def _cap_image_bytes(image_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(image_bytes)) as img:
        if max(img.size) <= MAX_IMAGE_SIDE:
            return image_bytes
        out = io.BytesIO()
        downscale(img).save(out, format="PNG")
        return out.getvalue()


async def _paginate_text(doc_id: str) -> None:
    data = await asyncio.to_thread(blob_path(doc_id).read_bytes)
    text = data.decode("utf-8", errors="replace")
    blocks = [Block(type="text", page_idx=0, text=part.strip()) for part in re.split(r"\n\s*\n", text) if part.strip()]
    await get_redis().hset(f"doc:{doc_id}", "page_count", 1)
    await record_page(doc_id, 0, blocks)


async def _paginate_html(doc_id: str) -> None:
    redis = get_redis()
    data = await asyncio.to_thread(blob_path(doc_id).read_bytes)
    raw_media = await redis.hgetall(normalize_media_key(doc_id))
    media = {key.decode(): value for key, value in raw_media.items()}
    await redis.delete(normalize_media_key(doc_id))
    blocks, images = await asyncio.to_thread(parse_html, data, media)
    resolved = await resolve_embedded_images(doc_id, "html", blocks, images)
    await redis.hset(f"doc:{doc_id}", "page_count", 1)
    await record_page(doc_id, 0, resolved)


async def _paginate_pptx(doc_id: str) -> None:
    data = await asyncio.to_thread(blob_path(doc_id).read_bytes)
    slides = await asyncio.to_thread(parse_pptx, data)
    await get_redis().hset(f"doc:{doc_id}", "page_count", max(len(slides), 1))
    for index, (slide_blocks, images) in enumerate(slides or [([], {})]):
        resolved = await resolve_embedded_images(doc_id, f"slide{index}", slide_blocks, images)
        await record_page(doc_id, index, resolved)


_BLOCK_PAGINATORS = {
    "text": _paginate_text,
    "html": _paginate_html,
    "html_pandoc": _paginate_html,
    "pptx": _paginate_pptx,
}


async def handle_paginate(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    kind = fields["kind"]
    redis = get_redis()
    await redis.hset(f"doc:{doc_id}", "kind", kind)
    await redis.hset(f"doc:{doc_id}", "state", "paginating")
    await redis.hsetnx(f"doc:{doc_id}", "t_paginate", time.time())
    paginator = _BLOCK_PAGINATORS.get(kind)
    if paginator is not None:
        await paginator(doc_id)
        return
    if kind.startswith("image:"):
        data = await asyncio.to_thread(blob_path(doc_id).read_bytes)
        image_bytes = await asyncio.to_thread(_cap_image_bytes, data)
        await redis.hset(f"doc:{doc_id}", "page_count", 1)
        await redis.set(page_image_key(doc_id, 0), image_bytes, ex=DOC_TTL)
        await redis.xadd(STREAM_PAGES, {"doc_id": doc_id, "page_idx": 0})
        return
    if kind in {"csv", "tsv", "json"}:
        await redis.hset(f"doc:{doc_id}", "mode", "tabular")
        await redis.hset(f"doc:{doc_id}", "page_count", 1)
        await redis.xadd(STREAM_TABLE_STRUCTURE, {"doc_id": doc_id, "unit": "sheet", "kind": kind, "sheet_no": 0})
        return
    dpi = RENDER_DPI
    count, _, _ = await _run_pdfium(count_pdf_pages, str(blob_path(doc_id)), job_timeout=RENDER_TIMEOUT)
    if count <= 0:
        await redis.hset(f"doc:{doc_id}", "page_count", 0)
        await get_redis().xadd(STREAM_MERGE, {"doc_id": doc_id})
        return
    await redis.hset(f"doc:{doc_id}", "page_count", count)
    stream = render_stream(doc_id)
    pipe = redis.pipeline(transaction=False)
    for idx in range(count):
        pipe.xadd(stream, {"doc_id": doc_id, "page_idx": idx, "dpi": dpi})
    await pipe.execute()
    await redis.sadd(RENDER_DOCS, doc_id)
    await redis.hsetnx(f"doc:{doc_id}", "t_paginated", time.time())


async def handle_render(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    page_idx = int(fields["page_idx"])
    dpi = fields["dpi"]
    path = blob_path(doc_id)
    if not path.exists():
        if not await _doc_terminal(doc_id):
            msg = f"source blob missing for in-flight doc {doc_id}"
            raise FileNotFoundError(msg)
        return
    redis = get_redis()
    await redis.hsetnx(f"doc:{doc_id}", "t_pages", time.time())
    await redis.xadd(STREAM_RASTERIZE, {"doc_id": doc_id, "page_idx": page_idx, "dpi": dpi})


async def handle_rasterize(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    page_idx = int(fields["page_idx"])
    dpi = int(fields["dpi"])
    path = blob_path(doc_id)
    if not path.exists():
        if not await _doc_terminal(doc_id):
            msg = f"source blob missing for in-flight doc {doc_id}"
            raise FileNotFoundError(msg)
        return
    image_bytes, render_wait, render_cpu = await _run_pdfium(
        render_pdf_page, str(path), page_idx, dpi, job_timeout=RENDER_TIMEOUT
    )
    await _add_stage_seconds(doc_id, "render_wait_s", render_wait)
    await _add_stage_seconds(doc_id, "render_s", render_cpu)
    redis = get_redis()
    await redis.set(page_image_key(doc_id, page_idx), image_bytes, ex=DOC_TTL)
    await redis.xadd(STREAM_PAGES, {"doc_id": doc_id, "page_idx": page_idx})


_RECORD_UNIT_LUA = """
if redis.call('HSETNX', KEYS[1], ARGV[1], ARGV[2]) == 0 then return 0 end
redis.call('EXPIRE', KEYS[1], ARGV[4])
redis.call('EXPIRE', KEYS[2], ARGV[4])
local done = redis.call('HINCRBY', KEYS[2], 'done_count', 1)
local expected = tonumber(redis.call('HGET', KEYS[2], 'page_count'))
if expected ~= nil and done == expected then
  redis.call('XADD', KEYS[3], '*', 'doc_id', ARGV[3])
  return 1
end
return 0
"""


@lru_cache
def _record_unit() -> AsyncScript:
    return get_redis().register_script(_RECORD_UNIT_LUA)


_REQUEUE_LUA = """
redis.call('XADD', KEYS[1], '*', unpack(ARGV, 3))
redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
redis.call('XDEL', KEYS[1], ARGV[2])
"""


@lru_cache
def _requeue() -> AsyncScript:
    return get_redis().register_script(_REQUEUE_LUA)


async def requeue_message(stream: str, msg_id: str, fields: dict[bytes, bytes]) -> None:
    flat = [item for pair in fields.items() for item in pair]
    await _requeue()(keys=[stream], args=[GROUP, msg_id, *flat])


async def record_page(doc_id: str, page_idx: int, blocks: list[Block]) -> None:
    await _record_unit()(
        keys=[f"blocks:{doc_id}", f"doc:{doc_id}", STREAM_STRUCTURE],
        args=[str(page_idx), json.dumps([b.model_dump() for b in blocks]), doc_id, str(DOC_TTL)],
    )


async def fail_page(doc_id: str, page_idx: int) -> None:
    await record_page(doc_id, page_idx, [Block(type="error", page_idx=page_idx, text="[extraction failed]")])


async def record_sheet(doc_id: str, sheet_no: int) -> None:
    await _record_unit()(
        keys=[f"sheets:{doc_id}", f"doc:{doc_id}", STREAM_MERGE], args=[str(sheet_no), "1", doc_id, str(DOC_TTL)]
    )


async def handle_tabular(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    kind = fields["kind"]
    sheet_no = int(fields["sheet_no"])
    redis = get_redis()
    await redis.hsetnx(f"doc:{doc_id}", "t_pages", time.time())
    await redis.hincrby(f"doc:{doc_id}", "started_count", 1)
    path = blob_path(doc_id)
    if not path.exists():
        if not await _doc_terminal(doc_id):
            msg = f"source blob missing for in-flight doc {doc_id}"
            raise FileNotFoundError(msg)
        return
    data = await asyncio.to_thread(path.read_bytes)
    filename = (await redis.hget(f"doc:{doc_id}", "filename") or b"").decode()
    if kind == "json":
        items = await asyncio.to_thread(extract_json_tables, data, filename.rsplit(".", 1)[0] or "root")
    else:
        separator = "\t" if kind == "tsv" else ","
        items = list(enumerate(await structure_csv_tables(data, separator), start=1))
    sheet_name = filename
    await save_sheet_tables(int(doc_id), sheet_no, sheet_name, items)
    await record_sheet(doc_id, sheet_no)


async def fail_document(doc_id: str, stage: str) -> None:
    redis = get_redis()
    now = time.time()
    await redis.hset(f"doc:{doc_id}", mapping={"state": "failed", "error": f"{stage} failed", "t_done": now})
    await mark_document(int(doc_id), DocumentStatus.FAILED)
    await cleanup(doc_id)


MAX_HEADING_LEVEL = 6


def _heading_level(line: str) -> int | None:
    stripped = line.strip()
    if not stripped.startswith("#"):
        return None
    level = len(stripped) - len(stripped.lstrip("#"))
    if level > MAX_HEADING_LEVEL or (len(stripped) > level and stripped[level] != " "):
        return None
    return level


def _split_code_fences(markdown: str) -> list[tuple[bool, str]]:
    segments: list[tuple[bool, str]] = []
    current: list[str] = []
    in_code = False
    for line in markdown.splitlines():
        if line.strip().startswith("```"):
            segments.append((in_code, "\n".join(current)))
            current = []
            in_code = not in_code
            continue
        current.append(line)
    segments.append((in_code, "\n".join(current)))
    return segments


def _blocks_from_plain_markdown(markdown: str) -> list[Block]:
    blocks: list[Block] = []
    paragraph: list[str] = []
    table: list[str] = []

    def flush_paragraph() -> None:
        text = "\n".join(paragraph).strip()
        if text:
            blocks.append(Block(page_idx=0, type="text", text=text))
        paragraph.clear()

    def flush_table() -> None:
        grid = grid_from_markdown("\n".join(table))
        if grid:
            blocks.append(Block(page_idx=0, type="table", grid=grid))
        table.clear()

    for line in markdown.splitlines():
        level = _heading_level(line)
        if level is not None:
            flush_paragraph()
            flush_table()
            text = line.strip().lstrip("#").strip()
            if text:
                blocks.append(Block(page_idx=0, type="title", text=text, text_level=level))
            continue
        if "|" in line:
            flush_paragraph()
            table.append(line)
            continue
        flush_table()
        if line.strip():
            paragraph.append(line)
        else:
            flush_paragraph()
    flush_paragraph()
    flush_table()
    return blocks


_TAG_MARKER = re.compile(r"^==([A-Z]+)==\s*$")
_TAGGED_TYPES = frozenset({"TEXT", "LIST", "TABLE", "FORM", "EQUATION", "CODE", "VISUAL", "PARATEXT", "EXTRA"})


def _split_by_tag(markdown: str) -> list[tuple[str, str]]:
    segments: list[tuple[str, str]] = []
    tag = "TEXT"
    lines: list[str] = []
    for line in markdown.splitlines():
        match = _TAG_MARKER.match(line.strip())
        if match and match.group(1) in _TAGGED_TYPES:
            if lines:
                segments.append((tag, "\n".join(lines)))
            tag = match.group(1)
            lines = []
        else:
            lines.append(line)
    if lines:
        segments.append((tag, "\n".join(lines)))
    return segments


def _paragraph_blocks(text: str, block_type: str) -> list[Block]:
    parts = re.split(r"\n\s*\n", text)
    return [Block(page_idx=0, type=block_type, text=part.strip()) for part in parts if part.strip()]


def _list_item_blocks(text: str) -> list[Block]:
    return [Block(page_idx=0, type="list_item", text=line.strip()) for line in text.splitlines() if line.strip()]


def _table_or_paragraph_blocks(text: str) -> list[Block]:
    grid = grid_from_markdown(text)
    if grid:
        return [Block(page_idx=0, type="table", grid=grid)]
    return _paragraph_blocks(text, "text")


_TAG_HANDLERS: dict[str, Callable[[str], list[Block]]] = {
    "TEXT": _blocks_from_plain_markdown,
    "EXTRA": _blocks_from_plain_markdown,
    "LIST": _list_item_blocks,
    "TABLE": _table_or_paragraph_blocks,
    "FORM": _table_or_paragraph_blocks,
    "CODE": lambda text: [Block(page_idx=0, type="code", text=text)],
    "EQUATION": lambda text: _paragraph_blocks(text, "equation"),
    "VISUAL": lambda text: _paragraph_blocks(text, "image"),
    "PARATEXT": lambda text: _paragraph_blocks(text, "header"),
}


def _blocks_from_tagged_segment(tag: str, text: str) -> list[Block]:
    if not text.strip():
        return []
    return _TAG_HANDLERS[tag](text)


def blocks_from_page_markdown(markdown: str) -> list[Block]:
    blocks: list[Block] = []
    for in_code, chunk in _split_code_fences(markdown):
        if in_code:
            if chunk.strip():
                blocks.append(Block(page_idx=0, type="code", text=chunk))
            continue
        for tag, segment in _split_by_tag(chunk):
            blocks.extend(_blocks_from_tagged_segment(tag, segment))
    return blocks


BLANK_STDDEV_THRESHOLD = 1.0


def _is_blank_image(image_bytes: bytes) -> bool:
    with Image.open(io.BytesIO(image_bytes)) as img:
        return ImageStat.Stat(img.convert("L")).stddev[0] < BLANK_STDDEV_THRESHOLD


_WRAP_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n(.*)\n```\s*$", re.DOTALL)
_THINK_TAG = re.compile(r"</?think>")

_META_LINE_PATTERNS = (
    re.compile(r"^here is the (content|text|transcription)\b", re.IGNORECASE),
    re.compile(r"^this transcription includes\b", re.IGNORECASE),
    re.compile(r"^i (cannot|can't|am unable to)\b", re.IGNORECASE),
    re.compile(r"^the (provided )?image (is|appears)\b.*(blank|empty)", re.IGNORECASE),
    re.compile(r"^there is no (visible )?(text|content)\b", re.IGNORECASE),
    re.compile(r"^therefore,.*(cannot|no markdown|no table)\b", re.IGNORECASE),
)


def _strip_think_tags(markdown: str) -> str:
    return _THINK_TAG.sub("", markdown)


def _strip_wrapping_fence(markdown: str) -> str:
    match = _WRAP_FENCE.match(markdown.strip())
    return match.group(1) if match else markdown


def _strip_meta_commentary(markdown: str) -> str:
    kept = [
        line
        for line in markdown.splitlines()
        if line.strip() != "---" and not any(pattern.match(line.strip()) for pattern in _META_LINE_PATTERNS)
    ]
    return "\n".join(kept)


async def _ocr_blocks(image_key: str) -> tuple[list[Block], float, float]:
    image_bytes = await get_redis().get(image_key)
    if isinstance(image_bytes, str):
        image_bytes = image_bytes.encode()
    if image_bytes is not None and await asyncio.to_thread(_is_blank_image, image_bytes):
        return [], 0.0, 0.0
    markdown, wait_s, gpu_s = await call_page_ocr(image_key)
    markdown = _strip_think_tags(markdown)
    markdown = _strip_wrapping_fence(markdown)
    markdown = _strip_meta_commentary(markdown)
    return blocks_from_page_markdown(markdown), wait_s, gpu_s


async def extract_page(doc_id: str, page_idx: int) -> tuple[list[Block], float, float]:
    return await _ocr_blocks(page_image_key(doc_id, page_idx))


def embedded_image_key(doc_id: str, unit: str, index: int) -> str:
    return f"embedded_image:{doc_id}:{unit}:{index}"


async def _resolve_unique_images(doc_id: str, unit: str, images: dict[int, bytes]) -> dict[int, list[Block]]:
    if not images:
        return {}
    redis = get_redis()
    cap = get_vision_capacity()
    normalized = {index: to_png_bytes(data) for index, data in images.items()}
    by_hash: dict[bytes, list[int]] = {}
    for index, data in normalized.items():
        by_hash.setdefault(sha256(data).digest(), []).append(index)
    keys = {digest: embedded_image_key(doc_id, unit, indices[0]) for digest, indices in by_hash.items()}
    await asyncio.gather(
        *(redis.set(keys[digest], normalized[indices[0]], ex=DOC_TTL) for digest, indices in by_hash.items())
    )

    async def resolve_one(digest: bytes) -> tuple[bytes, list[Block]]:
        key = keys[digest]
        await cap.acquire(key)
        try:
            blocks, _, _ = await _ocr_blocks(key)
            return digest, blocks
        finally:
            await cap.release(key)
            await redis.delete(key)

    resolved = dict(await asyncio.gather(*(resolve_one(digest) for digest in by_hash)))
    return {index: resolved[digest] for digest, indices in by_hash.items() for index in indices}


async def resolve_embedded_images(doc_id: str, unit: str, blocks: list[Block], images: dict[int, bytes]) -> list[Block]:
    resolved = await _resolve_unique_images(doc_id, unit, images)
    out: list[Block] = []
    for index, block in enumerate(blocks):
        if index in resolved:
            out.extend(resolved[index])
        else:
            out.append(block)
    return out


async def handle_ocr(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    page_idx = int(fields["page_idx"])
    await get_redis().hincrby(f"doc:{doc_id}", "started_count", 1)
    blocks, wait_s, gpu_s = await extract_page(doc_id, page_idx)
    for block in blocks:
        block.page_idx = page_idx
    await _add_stage_seconds(doc_id, "ocr_wait_s", wait_s)
    await _add_stage_seconds(doc_id, "ocr_gpu_s", gpu_s)
    await _emit_page(doc_id, page_idx, blocks)


async def _emit_page(doc_id: str, page_idx: int, blocks: list[Block]) -> None:
    await record_page(doc_id, page_idx, blocks)


STAGE_SECONDS = (
    ("render_wait", "render_wait_s"),
    ("render_cpu", "render_s"),
    ("ocr_wait", "ocr_wait_s"),
    ("ocr_gpu", "ocr_gpu_s"),
)


def _stage_line(doc: dict[bytes, bytes]) -> str:
    def g(key: str) -> float:
        return float(doc.get(key.encode(), 0) or 0)

    proc, paginate, paginated = g("t_proc"), g("t_paginate"), g("t_paginated")
    pages, merge, done = g("t_pages"), g("t_merge"), g("t_done")
    page_count = g("page_count")
    head = (("normalize", proc, paginate), ("paginate", paginate, paginated), ("waiting", paginated, pages))
    tail = (("pages_wall", pages, merge), ("merge", merge, done))
    walls = [f"{name}={end - start:.1f}s" for name, start, end in head if start and end]
    walls += [f"{name}={g(key) / page_count * 1000:.0f}ms/pg" for name, key in STAGE_SECONDS if g(key) and page_count]
    walls += [f"{name}={end - start:.1f}s" for name, start, end in tail if start and end]
    return " ".join(walls)


async def _read_doc_blocks(doc_id: str) -> list[Block]:
    per_page = await get_redis().hgetall(f"blocks:{doc_id}")
    blocks: list[Block] = []
    for page_idx in sorted(int(k) for k in per_page):
        blocks.extend(Block(**raw) for raw in json.loads(per_page[str(page_idx).encode()]))
    return blocks


_TEXT_SOURCE_KINDS = {"html", "html_pandoc", "pptx"}


async def _structured_table(
    doc_id: str, kind: str, index: int, page_idx: int, grid: list[list[str]] | None
) -> list[MaterializedTable]:
    if not grid:
        return []
    full = kind not in _TEXT_SOURCE_KINDS
    try:
        return await structure_candidate(grid, key=f"structure:{doc_id}:{index}", full=full)
    except json.JSONDecodeError:
        logger.exception("structure failed doc=%s index=%d page=%d", doc_id, index, page_idx)
        raise


async def handle_structure(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    redis = get_redis()
    kind = (await redis.hget(f"doc:{doc_id}", "kind") or b"").decode()
    blocks = await _read_doc_blocks(doc_id)
    prepared = prepare_document(blocks)
    await redis.set(f"structures:{doc_id}", dump_structures(prepared), ex=DOC_TTL)
    indices = table_block_indices(prepared.stitched)
    if indices:
        results = await asyncio.gather(
            *(
                _structured_table(doc_id, kind, index, prepared.stitched[index].page_idx, prepared.stitched[index].grid)
                for index in indices
            )
        )
        tables = {str(index): dump_tables(table_list) for index, table_list in zip(indices, results, strict=True)}
        await redis.hset(f"tables:{doc_id}", mapping=tables)
    await redis.xadd(STREAM_MERGE, {"doc_id": doc_id})


async def handle_table_structure(fields: dict[str, str]) -> None:
    await handle_tabular(fields)


async def handle_merge(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    redis = get_redis()
    now = time.time()
    await redis.hsetnx(f"doc:{doc_id}", "t_merge", now)
    if (await redis.hget(f"doc:{doc_id}", "mode") or b"").decode() == "tabular":
        await finalize_tabular(int(doc_id))
        await redis.hset(f"doc:{doc_id}", mapping={"state": "ingested", "t_done": time.time()})
        doc = await redis.hgetall(f"doc:{doc_id}")
        logger.info("merge doc_id=%s state=ingested tabular %s", doc_id, _stage_line(doc))
        return
    blocks = await _read_doc_blocks(doc_id)
    source = (await redis.hget(f"doc:{doc_id}", "filename") or b"").decode()
    state = DocumentStatus.PARTIAL if any(b.type == "error" for b in blocks) else DocumentStatus.INGESTED
    prepared = load_structures(await redis.get(f"structures:{doc_id}"))
    results = await redis.hgetall(f"tables:{doc_id}")
    table_counts, table_queue = collect_tables({int(key): value for key, value in results.items()})
    await save_document_tree(int(doc_id), blocks, state, prepared, table_counts, table_queue)
    await persist_document_tree(int(doc_id))
    await redis.hset(f"doc:{doc_id}", mapping={"state": state, "t_done": time.time()})
    doc = await redis.hgetall(f"doc:{doc_id}")
    t0 = float(doc.get(b"t0", 0) or 0)
    logger.info(
        "merge file=%s state=%s blocks=%d dur=%.1fs %s", source, state, len(blocks), time.time() - t0, _stage_line(doc)
    )


async def cleanup(doc_id: str) -> None:
    redis = get_redis()
    page_count = int(await redis.hget(f"doc:{doc_id}", "page_count") or 0)
    if page_count:
        await redis.delete(*(page_image_key(doc_id, page_idx) for page_idx in range(page_count)))
    await redis.delete(f"blocks:{doc_id}", f"sheets:{doc_id}", f"structures:{doc_id}", f"tables:{doc_id}")
    await redis.srem(RENDER_DOCS, doc_id)
    await redis.delete(render_stream(doc_id))
    blob_path(doc_id).unlink(missing_ok=True)
    await redis.expire(f"doc:{doc_id}", 3600)


async def ensure_group(stream: str) -> None:
    try:
        await get_redis().xgroup_create(stream, GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise

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
from collections import deque
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

import cv2
import numpy as np
from cachetools import LRUCache
from fastapi import HTTPException, UploadFile
from pebble import ProcessPool
from PIL import Image
from redis.commands.core import AsyncScript
from redis.exceptions import ResponseError
from sqlalchemy import select

from citadel.bus import get_redis
from citadel.db import get_sessionmaker
from citadel.models.document import Document
from citadel.models.status import DocumentStatus
from citadel.schemas.content import Block
from citadel.schemas.document import DocProgress, DocumentRead, IngestResponse
from citadel.services.detect import DetBlock, detect_layout
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
from citadel.services.excel import (
    SheetExtraction,
    extract_sheet_content,
    load_all_sheets,
    sheet_names,
)
from citadel.services.html import parse_html
from citadel.services.library import library_exists
from citadel.services.paddle import (
    CROP_CONCURRENCY,
    LAYER_LABELS,
    MIN_PIXELS,
    PICTURE_LABELS,
    PROMPT_OCR,
    block_text,
    close_vlm_clients,
    is_readable,
    otsl_to_grid,
    png_bytes,
    prompt_for,
    recognize,
    resize_for_vlm,
)
from citadel.services.pdf import (
    MAX_IMAGE_SIDE,
    count_pdf_pages,
    downscale,
    extract_layer_by_bbox,
    render_pdf_page,
    uncovered_layer_runs,
)
from citadel.services.presentation import parse_pptx
from citadel.services.tabular import extract_json_tables, grid_from_html, structure_csv_tables
from citadel.tabular.structure import structure_tables_ocr
from citadel.utils import normalize_file
from config import CPU_EIGHTH, CPU_THIRD

if TYPE_CHECKING:
    from citadel.tabular.materialize import MaterializedTable

logger = logging.getLogger(__name__)

GROUP = "citadel"
STREAM_INGEST = "ingest"
STREAM_NORMALIZED = "normalized"
RENDER_DOCS = "render:docs"
STREAM_PAGES = "pages"
STREAM_STRUCTURE = "structure"
STREAM_TABLE_STRUCTURE = "table_structure"
STREAM_MERGE = "merge"


def render_stream(doc_id: str) -> str:
    return f"render:{doc_id}"


MAX_ATTEMPTS = 3
DOC_TTL = 86_400
RENDER_DPI = 150
PAGINATE_CONCURRENCY = CPU_THIRD
RENDER_CONCURRENCY = CPU_EIGHTH
PDFIUM_WORKERS = 4
RENDER_TIMEOUT = 120
PDF_POOL_MAX_TASKS = 100
DETECT_WORKERS = 1
UNCHARGED_PAGES = 512
CROP_BUFFER = 12288
CROP_BOUND = CROP_CONCURRENCY + CROP_BUFFER
DECODE_CONCURRENCY = 16


@lru_cache
def get_decode_gate() -> asyncio.Semaphore:
    return asyncio.Semaphore(DECODE_CONCURRENCY)


@lru_cache
def get_crop_semaphore() -> asyncio.Semaphore:
    return asyncio.Semaphore(CROP_CONCURRENCY)


@dataclass
class _CropBudget:
    limit: int
    used: int = 0
    waiters: deque[tuple[int, asyncio.Future[None]]] = field(default_factory=deque)

    def free(self) -> int:
        return self.limit - self.used

    async def acquire(self, count: int) -> int:
        need = min(count, self.limit)
        if not self.waiters and self.free() >= need:
            self.used += need
            return need
        waiter = asyncio.get_running_loop().create_future()
        self.waiters.append((need, waiter))
        await waiter
        return need

    def release(self, count: int) -> None:
        self.used -= count
        while self.waiters:
            need, waiter = self.waiters[0]
            if waiter.done():
                self.waiters.popleft()
                continue
            if self.free() < need:
                return
            self.waiters.popleft()
            self.used += need
            waiter.set_result(None)


@dataclass
class _Admission:
    limit: int
    pending: set[tuple[str, int]] = field(default_factory=set)

    def free(self) -> int:
        return self.limit - len(self.pending)

    def enter(self, doc_id: str, page_idx: int) -> None:
        self.pending.add((doc_id, page_idx))

    def settle(self, doc_id: str, page_idx: int) -> None:
        self.pending.discard((doc_id, page_idx))


TIMELINE_BUCKET = 10.0
TIMELINE_TICK = 1.0
STARVED = CROP_CONCURRENCY // 2


@dataclass
class _Timeline:
    inflight: int = 0
    started: float = 0.0
    buckets: list[list[float]] = field(default_factory=list)

    def enter(self) -> None:
        self.inflight += 1
        if not self.started:
            self.started = time.time()

    def leave(self) -> None:
        self.inflight -= 1

    def sample(self, alive: int) -> None:
        if not self.started:
            return
        index = int((time.time() - self.started) // TIMELINE_BUCKET)
        while len(self.buckets) <= index:
            self.buckets.append([0.0, 0.0, 0.0])
        bucket = self.buckets[index]
        bucket[0] += self.inflight
        bucket[1] += alive
        bucket[2] += 1


@lru_cache
def get_timeline() -> _Timeline:
    return _Timeline()


async def sample_timeline() -> None:
    budget = get_crop_budget()
    while True:
        await asyncio.sleep(TIMELINE_TICK)
        get_timeline().sample(budget.used)


def log_timeline() -> None:
    timeline = get_timeline()
    points = [(flight / n, alive / n) for flight, alive, n in timeline.buckets if n]
    if not points:
        return
    starved = [(flight, alive) for flight, alive in points if flight < STARVED]
    logger.info(
        "crop timeline %.0fs/pt limit=%d mean=%.0f starved=%d/%d pts (crops_alive_then=%.0f) | %s",
        TIMELINE_BUCKET,
        CROP_CONCURRENCY,
        sum(flight for flight, _ in points) / len(points),
        len(starved),
        len(points),
        sum(alive for _, alive in starved) / len(starved) if starved else 0.0,
        " ".join(f"{flight:.0f}" for flight, _ in points),
    )
    get_timeline.cache_clear()


@lru_cache
def get_admission() -> _Admission:
    return _Admission(UNCHARGED_PAGES)


@lru_cache
def get_crop_budget() -> _CropBudget:
    return _CropBudget(CROP_BOUND)


T = TypeVar("T")
_PROCESS_POOLS: list[ProcessPool] = []


@lru_cache
def get_pdfium_pool() -> ProcessPool:
    ctx = multiprocessing.get_context("forkserver")
    ctx.set_forkserver_preload(["citadel.services.pdf"])
    pool = ProcessPool(max_workers=PDFIUM_WORKERS, max_tasks=PDF_POOL_MAX_TASKS, context=ctx)
    _PROCESS_POOLS.append(pool)
    return pool


@lru_cache
def get_pdfium_gate() -> asyncio.Semaphore:
    return asyncio.Semaphore(PDFIUM_WORKERS)


@lru_cache
def get_detect_pool() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=DETECT_WORKERS, mp_context=multiprocessing.get_context("spawn"))


@lru_cache
def get_detect_gate() -> asyncio.Semaphore:
    return asyncio.Semaphore(DETECT_WORKERS)


async def _run_detect(image: bytes) -> tuple[list[DetBlock], float, float]:
    loop = asyncio.get_running_loop()
    mark = time.time()
    async with get_detect_gate():
        wait = time.time() - mark
        mark = time.time()
        blocks = await loop.run_in_executor(get_detect_pool(), detect_layout, image)
        return blocks, wait, time.time() - mark


async def _run_pdfium[T](func: Callable[..., T], *args: object, job_timeout: float) -> tuple[T, float, float]:
    mark = time.time()
    async with get_pdfium_gate():
        wait = time.time() - mark
        mark = time.time()
        result = await asyncio.wrap_future(get_pdfium_pool().schedule(func, args=args, timeout=job_timeout))
        return result, wait, time.time() - mark


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
        pool.stop()
        pool.join()
    get_pdfium_pool.cache_clear()
    gc.collect()
    ctypes.CDLL("libc.so.6").malloc_trim(0)


async def shutdown() -> None:
    while _PROCESS_POOLS:
        pool = _PROCESS_POOLS.pop()
        pool.stop()
        pool.join()
    if get_detect_pool.cache_info().currsize:
        get_detect_pool().shutdown(wait=False, cancel_futures=True)
    await close_vlm_clients()
    _reap_profile_dirs()
    if get_redis.cache_info().currsize:
        await get_redis().aclose()


BLOB_DIR = Path(tempfile.gettempdir()) / "citadel-blobs"


async def _add_stage_seconds(doc_id: str, field: str, seconds: float) -> None:
    await get_redis().hincrbyfloat(f"doc:{doc_id}", field, seconds)


def blob_path(doc_id: str | int) -> Path:
    return BLOB_DIR / str(doc_id)


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


async def handle_normalize(fields: dict[str, str], profile_dir: str) -> None:
    doc_id = fields["doc_id"]
    redis = get_redis()
    await redis.hset(f"doc:{doc_id}", "state", "normalizing")
    await redis.hsetnx(f"doc:{doc_id}", "t_proc", time.time())
    await mark_processing(int(doc_id))
    data = await asyncio.to_thread(blob_path(doc_id).read_bytes)
    kind, normalized = await asyncio.to_thread(normalize_file, data, fields["filename"], profile_dir)
    await asyncio.to_thread(blob_path(doc_id).write_bytes, normalized)
    await get_redis().xadd(STREAM_NORMALIZED, {"doc_id": doc_id, "kind": kind, "filename": fields["filename"]})
    logger.info("normalize file=%s kind=%s", fields["filename"], kind)


def _cap_image_bytes(image_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(image_bytes)) as img:
        if max(img.size) <= MAX_IMAGE_SIDE:
            return image_bytes
        out = io.BytesIO()
        downscale(img).save(out, format="PNG")
        return out.getvalue()


async def _paginate_text(doc_id: str, filename: str) -> None:
    data = await asyncio.to_thread(blob_path(doc_id).read_bytes)
    text = data.decode("utf-8", errors="replace")
    blocks = [Block(type="text", page_idx=0, text=part.strip()) for part in re.split(r"\n\s*\n", text) if part.strip()]
    await get_redis().hset(f"doc:{doc_id}", "page_count", 1)
    await record_page(doc_id, 0, blocks)
    logger.info("paginate file=%s text paragraphs=%d", filename, len(blocks))


async def _paginate_html(doc_id: str, filename: str) -> None:
    data = await asyncio.to_thread(blob_path(doc_id).read_bytes)
    blocks = await asyncio.to_thread(parse_html, data)
    await get_redis().hset(f"doc:{doc_id}", "page_count", 1)
    await record_page(doc_id, 0, blocks)
    logger.info("paginate file=%s html blocks=%d", filename, len(blocks))


async def _paginate_pptx(doc_id: str, filename: str) -> None:
    data = await asyncio.to_thread(blob_path(doc_id).read_bytes)
    slides = await asyncio.to_thread(parse_pptx, data)
    await get_redis().hset(f"doc:{doc_id}", "page_count", max(len(slides), 1))
    for index, slide_blocks in enumerate(slides or [[]]):
        await record_page(doc_id, index, slide_blocks)
    logger.info("paginate file=%s pptx slides=%d", filename, len(slides))


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
        await paginator(doc_id, fields["filename"])
        return
    if kind.startswith("image:"):
        data = await asyncio.to_thread(blob_path(doc_id).read_bytes)
        image_bytes = await asyncio.to_thread(_cap_image_bytes, data)
        await redis.hset(f"doc:{doc_id}", "page_count", 1)
        await redis.xadd(STREAM_PAGES, {"doc_id": doc_id, "page_idx": 0, "image": image_bytes})
        logger.info("paginate file=%s image", fields["filename"])
        return
    if kind in {"xlsx", "csv", "tsv", "json"}:
        await redis.hset(f"doc:{doc_id}", "mode", "tabular")
        if kind == "xlsx":
            names = await asyncio.to_thread(sheet_names, await asyncio.to_thread(blob_path(doc_id).read_bytes))
            if not names:
                await redis.hset(f"doc:{doc_id}", "page_count", 0)
                await redis.xadd(STREAM_MERGE, {"doc_id": doc_id})
                logger.info("paginate file=%s sheets=0 → merge", fields["filename"])
                return
            await redis.hset(f"doc:{doc_id}", "page_count", len(names))
            for sheet_no in range(1, len(names) + 1):
                await redis.xadd(
                    STREAM_TABLE_STRUCTURE, {"doc_id": doc_id, "unit": "sheet", "kind": kind, "sheet_no": sheet_no}
                )
            logger.info("paginate file=%s sheets=%d", fields["filename"], len(names))
        else:
            await redis.hset(f"doc:{doc_id}", "page_count", 1)
            await redis.xadd(STREAM_TABLE_STRUCTURE, {"doc_id": doc_id, "unit": "sheet", "kind": kind, "sheet_no": 0})
            logger.info("paginate file=%s kind=%s", fields["filename"], kind)
        return
    dpi = RENDER_DPI
    count, _, _ = await _run_pdfium(count_pdf_pages, str(blob_path(doc_id)), job_timeout=RENDER_TIMEOUT)
    if count <= 0:
        await redis.hset(f"doc:{doc_id}", "page_count", 0)
        await get_redis().xadd(STREAM_MERGE, {"doc_id": doc_id})
        logger.info("paginate file=%s pages=0 → merge", fields["filename"])
        return
    await redis.hset(f"doc:{doc_id}", "page_count", count)
    stream = render_stream(doc_id)
    pipe = redis.pipeline(transaction=False)
    for idx in range(count):
        pipe.xadd(stream, {"doc_id": doc_id, "page_idx": idx, "dpi": dpi})
    await pipe.execute()
    await redis.sadd(RENDER_DOCS, doc_id)
    await redis.hsetnx(f"doc:{doc_id}", "t_paginated", time.time())
    logger.info("paginate file=%s pages=%d", fields["filename"], count)


async def handle_render(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    page_idx = int(fields["page_idx"])
    dpi = int(fields["dpi"])
    path = blob_path(doc_id)
    if not path.exists():
        if not await _doc_terminal(doc_id):
            msg = f"source blob missing for in-flight doc {doc_id}"
            raise FileNotFoundError(msg)
        return
    redis = get_redis()
    await redis.hsetnx(f"doc:{doc_id}", "t_pages", time.time())
    (image_bytes, digital), render_wait, render_cpu = await _run_pdfium(
        render_pdf_page, str(path), page_idx, dpi, job_timeout=RENDER_TIMEOUT
    )
    await _add_stage_seconds(doc_id, "render_wait_s", render_wait)
    await _add_stage_seconds(doc_id, "render_s", render_cpu)
    await redis.xadd(
        STREAM_PAGES,
        {"doc_id": doc_id, "page_idx": page_idx, "image": image_bytes, "digital": "1" if digital else "0"},
    )


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


async def render_weights(docs: list[str]) -> dict[str, float]:
    redis = get_redis()
    pipe = redis.pipeline(transaction=False)
    for doc_id in docs:
        pipe.hmget(f"doc:{doc_id}", "page_count", "done_count", "crops_n")
    rows = await pipe.execute()
    stats: dict[str, tuple[float, float, float]] = {}
    seen_crops = seen_pages = 0.0
    for doc_id, row in zip(docs, rows, strict=True):
        pages, done, crops = (float(value or 0) for value in row)
        stats[doc_id] = (pages, done, crops)
        seen_crops += crops
        seen_pages += done
    mean_density = seen_crops / seen_pages if seen_pages else 1.0
    weights: dict[str, float] = {}
    for doc_id, (pages, done, crops) in stats.items():
        density = crops / done if done else mean_density
        weights[doc_id] = max(1.0, (pages - done) * max(density, 1.0))
    return weights


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


SHEETS_CACHE_MAX = 4
_SHEETS_CACHE: LRUCache[str, list[SheetExtraction]] = LRUCache(maxsize=SHEETS_CACHE_MAX)
_SHEETS_LOCK = asyncio.Lock()


async def _get_sheet(doc_id: str, sheet_no: int) -> SheetExtraction:
    async with _SHEETS_LOCK:
        sheets = _SHEETS_CACHE.get(doc_id)
        if sheets is None:
            data = await asyncio.to_thread(blob_path(doc_id).read_bytes)
            sheets = await asyncio.to_thread(load_all_sheets, data)
            _SHEETS_CACHE[doc_id] = sheets
    return sheets[sheet_no - 1]


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
    if kind == "xlsx":
        sheet = await _get_sheet(doc_id, sheet_no)
        items = await extract_sheet_content(sheet)
        sheet_name = sheet.sheet_name
    else:
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


FILLIN = re.compile(r"\.{4,}|_{4,}|…")


def _is_vlm_refusal(text: str) -> bool:
    t = text.lower()
    refers = any(k in t for k in ("the image", "this image", "the picture", "image provided", "qr code", "barcode"))
    denies = any(
        k in t
        for k in (
            "no text",
            "no visible",
            "no textual",
            "human-readable",
            "does not contain",
            "cannot be",
            "can't be",
            "unable to",
            "be extracted",
            "be converted",
            "be processed",
        )
    )
    return refers and denies


@dataclass
class _Spans:
    decode_wait: float = 0.0
    layout_wait: float = 0.0
    layout: float = 0.0
    budget_wait: float = 0.0
    cut: float = 0.0
    crop_wait: float = 0.0
    predict: float = 0.0
    crops: int = 0


SPAN_FIELDS = ("decode_wait", "layout_wait", "layout", "budget_wait", "cut", "crop_wait", "predict")


async def _record_spans(doc_id: str, spans: _Spans) -> None:
    for field_name in SPAN_FIELDS:
        await _add_stage_seconds(doc_id, f"{field_name}_s", getattr(spans, field_name))
    await _add_stage_seconds(doc_id, "crops_n", float(spans.crops))


def reading_order(blocks: list[DetBlock]) -> list[DetBlock]:
    ordered = sorted((b for b in blocks if b.order is not None), key=lambda b: b.order or 0)
    loose = sorted((b for b in blocks if b.order is None), key=lambda b: (b.bbox[1], b.bbox[0]))
    out = list(ordered)
    for block in loose:
        at = next((i for i, other in enumerate(out) if other.bbox[1] > block.bbox[1]), len(out))
        out.insert(at, block)
    return out


def _to_read(blocks: list[DetBlock], digital: bool) -> list[int]:
    skip = LAYER_LABELS if digital else frozenset()
    return [i for i, b in enumerate(blocks) if is_readable(b) and b.label not in skip]


GAP_GRID = 24
GAP_CELL_PX = 6
GAP_MIN_AREA_FRACTION = 0.04
GAP_CONTENT_STD = 2.0


def _occupancy(blocks: list[DetBlock]) -> np.ndarray:
    covered = np.zeros((GAP_GRID, GAP_GRID), dtype=bool)
    for block in blocks:
        x0, y0, x1, y1 = block.bbox
        col0, col1 = int(x0 * GAP_GRID), min(GAP_GRID, max(int(x0 * GAP_GRID) + 1, int(x1 * GAP_GRID)))
        row0, row1 = int(y0 * GAP_GRID), min(GAP_GRID, max(int(y0 * GAP_GRID) + 1, int(y1 * GAP_GRID)))
        covered[row0:row1, col0:col1] = True
    return covered


def _coverage_gap(img: Image.Image, blocks: list[DetBlock]) -> tuple[float, float] | None:
    small_side = GAP_GRID * GAP_CELL_PX
    small = np.asarray(img.convert("L").resize((small_side, small_side), Image.Resampling.BILINEAR), dtype=np.float64)
    cell_std = small.reshape(GAP_GRID, GAP_CELL_PX, GAP_GRID, GAP_CELL_PX).std(axis=(1, 3))
    uncovered = ~_occupancy(blocks)
    fraction = float(uncovered.mean())
    if fraction < GAP_MIN_AREA_FRACTION:
        return None
    content = float(cell_std[uncovered].mean())
    return (fraction, content) if content >= GAP_CONTENT_STD else None


def _cut_one(img: Image.Image, bbox: list[float]) -> Image.Image:
    width, height = img.size
    box = (int(bbox[0] * width), int(bbox[1] * height), int(bbox[2] * width), int(bbox[3] * height))
    return img.crop((box[0], box[1], max(box[2], box[0] + 1), max(box[3], box[1] + 1)))


MERGEABLE_LABELS = frozenset({"text", "footnote", "reference_content", "content", "abstract"})


def _union(first: list[float], second: list[float]) -> list[float]:
    return [
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    ]


def _bbox_pixels(bbox: list[float], size: tuple[int, int]) -> int:
    return max(1, int((bbox[2] - bbox[0]) * size[0])) * max(1, int((bbox[3] - bbox[1]) * size[1]))


COLUMN_WIDEN = 1.25


def _stacked(group: list[float], bbox: list[float]) -> bool:
    union = max(group[2], bbox[2]) - min(group[0], bbox[0])
    widest = max(group[2] - group[0], bbox[2] - bbox[0])
    return union <= widest * COLUMN_WIDEN


@dataclass
class _GroupCloses:
    joined: int = 0
    area: int = 0
    label: int = 0
    gap: int = 0
    column: int = 0
    first: int = 0


@lru_cache
def get_group_closes() -> _GroupCloses:
    return _GroupCloses()


def log_group_closes() -> None:
    closes = get_group_closes()
    total = closes.joined + closes.area + closes.label + closes.gap + closes.first
    if not total:
        return
    blocked = max(1, closes.area + closes.label + closes.gap + closes.column)
    logger.info(
        "group closes joined=%d area=%d label=%d gap=%d column=%d first=%d — blocked by area %.1f%% / label %.1f%%"
        " / column %.1f%%",
        closes.joined,
        closes.area,
        closes.label,
        closes.gap,
        closes.column,
        closes.first,
        100 * closes.area / blocked,
        100 * closes.label / blocked,
        100 * closes.column / blocked,
    )
    get_group_closes.cache_clear()


def group_crops(blocks: list[DetBlock], indices: list[int], size: tuple[int, int]) -> list[list[int]]:
    groups: list[list[int]] = []
    unions: list[list[float]] = []
    for index in indices:
        label = blocks[index].label
        bbox = list(blocks[index].bbox)
        closes = get_group_closes()
        if not groups:
            closes.first += 1
        elif label not in MERGEABLE_LABELS or blocks[groups[-1][-1]].label != label:
            closes.label += 1
        elif index != groups[-1][-1] + 1:
            closes.gap += 1
        elif not _stacked(unions[-1], bbox):
            closes.column += 1
        else:
            candidate = _union(unions[-1], bbox)
            if _bbox_pixels(candidate, size) <= MIN_PIXELS:
                groups[-1].append(index)
                unions[-1] = candidate
                closes.joined += 1
                continue
            closes.area += 1
        groups.append([index])
        unions.append(bbox)
    return groups


def group_bbox(blocks: list[DetBlock], group: list[int]) -> list[float]:
    bbox = list(blocks[group[0]].bbox)
    for index in group[1:]:
        bbox = _union(bbox, list(blocks[index].bbox))
    return bbox


def _cut_crops(img: Image.Image, blocks: list[DetBlock], groups: list[list[int]]) -> list[bytes]:
    payloads: list[bytes] = []
    for group in groups:
        crop = _cut_one(img, group_bbox(blocks, group))
        sized = resize_for_vlm(crop)
        payloads.append(png_bytes(sized))
        if sized is not crop:
            sized.close()
        crop.close()
    return payloads


async def _read_crop(
    payload: bytes, prompt: str, semaphore: asyncio.Semaphore, budget: _CropBudget, spans: _Spans
) -> str:
    mark = time.time()
    timeline = get_timeline()
    try:
        async with semaphore:
            spans.crop_wait += time.time() - mark
            mark = time.time()
            timeline.enter()
            try:
                text = await recognize(payload, prompt)
            finally:
                timeline.leave()
            spans.predict += time.time() - mark
            return text
    finally:
        budget.release(1)


async def extract_page(doc_id: str, page_idx: int, image: bytes, digital: bool) -> list[Block]:
    budget = get_crop_budget()
    semaphore = get_crop_semaphore()
    spans = _Spans()

    blocks, spans.layout_wait, spans.layout = await _run_detect(image)
    blocks = reading_order(blocks)
    indices = _to_read(blocks, digital)

    mark = time.time()
    granted = await budget.acquire(max(len(indices), 1))
    get_admission().settle(doc_id, page_idx)
    spans.budget_wait = time.time() - mark
    mark = time.time()
    async with get_decode_gate():
        spans.decode_wait = 0.0
        with Image.open(io.BytesIO(image)) as img:
            groups = group_crops(blocks, indices, img.size)
            try:
                payloads = await asyncio.to_thread(_cut_crops, img, blocks, groups)
            except BaseException:
                budget.release(granted)
                raise
    spans.cut = time.time() - mark
    spans.crops = len(payloads)
    budget.release(granted - len(payloads))

    texts = await asyncio.gather(
        *(
            _read_crop(payload, prompt_for(blocks[group[0]].label), semaphore, budget, spans)
            for group, payload in zip(groups, payloads, strict=True)
        )
    )
    await _record_spans(doc_id, spans)
    return _blocks_from_groups(blocks, groups, texts)


def _blocks_from_groups(blocks: list[DetBlock], groups: list[list[int]], texts: list[str]) -> list[Block]:
    read = {group[0]: (group, text) for group, text in zip(groups, texts, strict=True)}
    folded = {index for group in groups for index in group[1:]}
    out: list[Block] = []
    for index, block in enumerate(blocks):
        if index in folded:
            continue
        group, text = read.get(index, ([index], ""))
        out.append(
            Block(
                type=block.label,
                page_idx=0,
                bbox=group_bbox(blocks, group),
                text=block_text(block.label, text),
                grid=otsl_to_grid(text) if block.label == "table" else None,
            )
        )
    return out


async def _reread(image: bytes, blocks: list[Block]) -> None:
    if not blocks:
        return
    budget = get_crop_budget()
    semaphore = get_crop_semaphore()
    spans = _Spans()
    granted = await budget.acquire(len(blocks))
    async with get_decode_gate():
        with Image.open(io.BytesIO(image)) as img:
            try:
                payloads = await asyncio.to_thread(
                    _cut_crops,
                    img,
                    [DetBlock(label=b.type, score=1.0, bbox=list(b.bbox or []), order=None) for b in blocks],
                    [[index] for index in range(len(blocks))],
                )
            except BaseException:
                budget.release(granted)
                raise
    budget.release(granted - len(payloads))
    texts = await asyncio.gather(*(_read_crop(payload, PROMPT_OCR, semaphore, budget, spans) for payload in payloads))
    for block, text in zip(blocks, texts, strict=True):
        clean = text.strip()
        if clean and not _is_vlm_refusal(clean):
            block.text = clean


@lru_cache
def _qr_detector() -> cv2.QRCodeDetector:
    return cv2.QRCodeDetector()


def _qr_payload(crop: Image.Image) -> str | None:
    text, points, _ = _qr_detector().detectAndDecode(np.asarray(crop.convert("RGB")))
    return text if points is not None else None


def _qr_scan(img: Image.Image, blocks: list[Block]) -> int:
    decoded = 0
    for block in blocks:
        if block.type not in PICTURE_LABELS or (block.text or "").strip():
            continue
        crop = _cut_one(img, list(block.bbox or []))
        try:
            payload = _qr_payload(crop)
        finally:
            crop.close()
        if payload:
            block.text = payload
            decoded += 1
    return decoded


async def read_pictures(image: bytes, blocks: list[Block]) -> int:
    async with get_decode_gate():
        with Image.open(io.BytesIO(image)) as img:
            return _qr_scan(img, blocks)


async def recover_fillin_blocks(image: bytes, blocks: list[Block]) -> int:
    flagged = [
        b for b in blocks if b.type in LAYER_LABELS and (not (b.text or "").strip() or FILLIN.search(b.text or ""))
    ]
    await _reread(image, flagged)
    return len(flagged)


CROP_LOG_THRESHOLD = 6


async def _rescue_uncovered(doc_id: str, path: Path, page_idx: int, blocks: list[Block]) -> tuple[list[Block], int]:
    runs, wait, cpu = await _run_pdfium(
        uncovered_layer_runs,
        str(path),
        page_idx,
        [list(block.bbox or []) for block in blocks],
        job_timeout=RENDER_TIMEOUT,
    )
    await _add_stage_seconds(doc_id, "layer_wait_s", wait)
    await _add_stage_seconds(doc_id, "layer_s", cpu)
    if not runs:
        return blocks, 0
    merged = list(blocks)
    for run in runs:
        block = Block(type="text", page_idx=0, bbox=list(run.bbox), text=run.text)
        at = next((i for i, other in enumerate(merged) if (other.bbox or [0, 0, 0, 0])[1] > run.bbox[1]), len(merged))
        merged.insert(at, block)
    return merged, len(runs)


async def handle_ocr(fields: dict[str, str], image: bytes) -> None:
    doc_id = fields["doc_id"]
    page_idx = int(fields["page_idx"])
    digital = fields.get("digital") == "1"
    await get_redis().hincrby(f"doc:{doc_id}", "started_count", 1)
    ocr_t = time.time()
    blocks = await extract_page(doc_id, page_idx, image, digital)
    for block in blocks:
        block.page_idx = page_idx

    fill_crops = 0
    rescued = 0
    if digital:
        layer_blocks = [b for b in blocks if b.type in LAYER_LABELS]
        path = blob_path(doc_id)
        if layer_blocks and path.exists():
            layer, layer_wait, layer_cpu = await _run_pdfium(
                extract_layer_by_bbox,
                str(path),
                page_idx,
                [list(b.bbox or []) for b in layer_blocks],
                job_timeout=RENDER_TIMEOUT,
            )
            await _add_stage_seconds(doc_id, "layer_wait_s", layer_wait)
            await _add_stage_seconds(doc_id, "layer_s", layer_cpu)
            for block, text in zip(layer_blocks, layer, strict=True):
                if text:
                    block.text = text
        if path.exists():
            blocks, rescued = await _rescue_uncovered(doc_id, path, page_idx, blocks)
        fill_crops = await recover_fillin_blocks(image, blocks)

    img_crops = await read_pictures(image, blocks)
    await _add_stage_seconds(doc_id, "ocr_s", time.time() - ocr_t)
    if rescued:
        await get_redis().hincrby(f"doc:{doc_id}", "rescued_pages", 1)
        await get_redis().hincrby(f"doc:{doc_id}", "rescued_runs", rescued)
    if img_crops + fill_crops >= CROP_LOG_THRESHOLD:
        await get_redis().hincrby(f"doc:{doc_id}", "ocr_heavy_pages", 1)
    await _emit_page(doc_id, page_idx, blocks)


async def _emit_page(doc_id: str, page_idx: int, blocks: list[Block]) -> None:
    await record_page(doc_id, page_idx, blocks)


STAGE_SECONDS = (
    ("render_wait", "render_wait_s", "pg"),
    ("render_cpu", "render_s", "pg"),
    ("ocr_wall", "ocr_s", "pg"),
    ("decode_wait", "decode_wait_s", "pg"),
    ("layout_wait", "layout_wait_s", "pg"),
    ("layout", "layout_s", "pg"),
    ("budget_wait", "budget_wait_s", "pg"),
    ("cut", "cut_s", "pg"),
    ("crop_wait", "crop_wait_s", "crop"),
    ("predict", "predict_s", "crop"),
    ("layer_wait", "layer_wait_s", "pg"),
    ("layer_cpu", "layer_s", "pg"),
)


def _stage_line(doc: dict[bytes, bytes]) -> str:
    def g(key: str) -> float:
        return float(doc.get(key.encode(), 0) or 0)

    proc, paginate, paginated = g("t_proc"), g("t_paginate"), g("t_paginated")
    pages, merge, done = g("t_pages"), g("t_merge"), g("t_done")
    units = {"pg": g("page_count"), "crop": g("crops_n")}
    head = (("normalize", proc, paginate), ("paginate", paginate, paginated), ("queued", paginated, pages))
    tail = (("pages_wall", pages, merge), ("merge", merge, done))
    walls = [f"{name}={end - start:.1f}s" for name, start, end in head if start and end]
    walls += [
        f"{name}={g(key) / units[unit] * 1000:.0f}ms/{unit}"
        for name, key, unit in STAGE_SECONDS
        if g(key) and units[unit]
    ]
    walls += [f"crops={g('crops_n'):.0f}"]
    if g("rescued_pages"):
        walls += [f"rescued={g('rescued_pages'):.0f}pg/{g('rescued_runs'):.0f}run"]
    if g("ocr_heavy_pages"):
        walls += [f"ocr_heavy={g('ocr_heavy_pages'):.0f}pg"]
    walls += [f"{name}={end - start:.1f}s" for name, start, end in tail if start and end]
    return " ".join(walls)


async def _read_doc_blocks(doc_id: str) -> list[Block]:
    per_page = await get_redis().hgetall(f"blocks:{doc_id}")
    blocks: list[Block] = []
    for page_idx in sorted(int(k) for k in per_page):
        blocks.extend(Block(**raw) for raw in json.loads(per_page[str(page_idx).encode()]))
    return blocks


async def handle_structure(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    redis = get_redis()
    proc, pages, crops = (
        float(value or 0) for value in await redis.hmget(f"doc:{doc_id}", "t_proc", "page_count", "crops_n")
    )
    elapsed = time.time() - proc if proc else 0.0
    logger.info("ocr_complete doc=%s elapsed=%.1fs pages=%.0f crops=%.0f", doc_id, elapsed, pages, crops)
    blocks = await _read_doc_blocks(doc_id)
    prepared = prepare_document(blocks)
    await redis.set(f"structures:{doc_id}", dump_structures(prepared), ex=DOC_TTL)
    indices = table_block_indices(prepared.stitched)
    if not indices:
        await redis.xadd(STREAM_MERGE, {"doc_id": doc_id})
        return
    await redis.xadd(STREAM_TABLE_STRUCTURE, {"doc_id": doc_id, "unit": "document"})


async def handle_table_structure(fields: dict[str, str]) -> None:
    if fields["unit"] == "sheet":
        await handle_tabular(fields)
        return
    doc_id = fields["doc_id"]
    redis = get_redis()
    prepared = load_structures(await redis.get(f"structures:{doc_id}"))
    indices = table_block_indices(prepared.stitched)
    grids = [prepared.stitched[index].grid or grid_from_html(prepared.stitched[index].text or "") for index in indices]
    structured = await structure_tables_ocr(grids, label=doc_id)
    by_block: dict[int, list[MaterializedTable]] = {}
    for table, blocks in structured:
        by_block.setdefault(indices[min(blocks)], []).append(table)
    await redis.hset(
        f"tables:{doc_id}", mapping={str(index): dump_tables(by_block.get(index, [])) for index in indices}
    )
    await redis.xadd(STREAM_MERGE, {"doc_id": doc_id})


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
    _SHEETS_CACHE.pop(doc_id, None)
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

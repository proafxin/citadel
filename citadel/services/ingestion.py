import asyncio
import ctypes
import gc
import io
import itertools
import json
import logging
import multiprocessing
import re
import shutil
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TypeVar

import cv2
import numpy as np
import redis.asyncio as aioredis
from cachetools import LRUCache
from fastapi import HTTPException, UploadFile
from mineru_vl_utils import MinerUClient
from mineru_vl_utils.mineru_client import _PredictResult
from mineru_vl_utils.structs import ContentBlock, ExtractResult
from mineru_vl_utils.vlm_client import SamplingParams
from mineru_vl_utils.vlm_client.utils import get_png_bytes
from pebble import ProcessPool
from PIL import Image
from rapidocr_onnxruntime import RapidOCR
from redis.backoff import ExponentialBackoff
from redis.commands.core import AsyncScript
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.retry import Retry
from sqlalchemy import select

from citadel.db import get_sessionmaker
from citadel.models.document import Document
from citadel.models.status import DocumentStatus
from citadel.schemas.content import Block
from citadel.schemas.document import DocProgress, DocumentRead, IngestResponse
from citadel.services.document import (
    begin_library_ingest,
    create_documents,
    finalize_tabular,
    mark_document,
    mark_processing,
    persist_document_tree,
    save_document_tree,
    save_sheet_tables,
)
from citadel.services.excel import SheetExtraction, extract_sheet_content, load_all_sheets, sheet_names
from citadel.services.html import parse_html
from citadel.services.library import library_exists
from citadel.services.pdf import MAX_IMAGE_SIDE, count_pdf_pages, downscale, extract_layer_by_bbox, render_pdf_page
from citadel.services.presentation import parse_pptx
from citadel.services.tabular import extract_json_tables, structure_csv_tables
from citadel.tabular.infer import release_header_pool
from citadel.utils import normalize_file
from config import CPU_EIGHTH, CPU_THIRD, get_settings

logger = logging.getLogger(__name__)

GROUP = "citadel"
STREAM_INGEST = "ingest"
STREAM_NORMALIZED = "normalized"
STREAM_RENDER = "render"  # per-page render jobs: one message per PDF page, drained by the bounded render consumer
STREAM_PAGES = "pages"
STREAM_MERGE = "merge"
STREAM_TABLES = "tables"
STREAM_GAPFILL = "gapfill"  # decoupled CPU stage: scanned pages do RapidOCR gap-fill here, off the GPU OCR slot


MAX_ATTEMPTS = 3
DOC_TTL = 86_400  # safety expiry on doc/blocks/sheets keys: set at submit, refreshed on every recorded unit, so a doc
# that somehow never reaches merge (and so never hits cleanup) still self-evicts instead of accumulating in Redis
# forever. far longer than any single doc's processing, so it never evicts live state; cleanup shortens it on finish.
LAYER_TYPES = ("text", "title")  # filled from the PDF text layer on born-digital pages (skip VLM recognition)

MINERU_CLIENTS = 16
MINERU_CONN_PER_CLIENT = (
    32  # sockets, NOT a concurrency bound — it must stay well ABOVE CROP_CONCURRENCY/MINERU_CLIENTS
)
# or it silently becomes one. httpx blocks a request with no free connection INSIDE client.post, so it holds its crop
# permit and sends nothing. round-robin is even by request COUNT but not by DURATION — a layout call holds its socket
# ~20x longer than a crop (124s vs 6s) — so pools saturate unevenly and requests queue behind a busy client while other
# clients sit idle. when this was CROP_CONCURRENCY/MINERU_CLIENTS exactly, mineru saw Running ~130 / Waiting 0 against a
# semaphore of 256: half the GPU idle with thousands of crops ready to send. 2x headroom makes the semaphore the only bound
REDIS_MAX_CONNECTIONS = 64  # bounded blocking pool: callers queue for a connection, never open unbounded sockets
RENDER_DPI = 150  # validated equal to 200 (the VLM resizes internally) and ~26% faster
PAGINATE_CONCURRENCY = CPU_EIGHTH
RENDER_CONCURRENCY = CPU_EIGHTH
PDFIUM_WORKERS = 1  # every pdfium job (count + render + text layer) shares these. measured ~650MB standing per worker
# (live heap, stable across max_tasks recycles, never reproduced outside the pool), so this is the most expensive
# process we own per unit of work done — and the work is trivial: the 809-page book spent 165s of render across an 800s
# wall, so one worker runs at ~20% duty. the stage is a FIFO off the GPU path; finishing a page sooner only makes it
# wait longer in the buffer, so there is nothing to buy by widening it
RENDER_TIMEOUT = 120  # page renders in <1s; if a job strands (dead/hung pool worker) free the slot + retry the page
PDF_POOL_MAX_TASKS = 100  # recycle a pdfium worker every N pages: a long-lived one grows to hundreds of MB and never
# gives it back, and there are 12 of them. respawn is a fork off the (lean) forkserver, so the cost is milliseconds
# amortised over 100 pages, against a stage that is already the cheapest in the pipeline
RAPIDOCR_CONCURRENCY = CPU_THIRD  # scanned-page gap-OCR threads (CPU); bounds RapidOCR so it can't starve
GAP_FILL = True  # RapidOCR scanned gap-fill; set to False for clean-image benchmarks (pure VLM)
CROP_CONCURRENCY = 128  # a crop is the unit of VLM work; this semaphore caps what is outstanding at the model at all
# (Running + Waiting). it bounds MEMORY, not throughput: a queued request pins its decoded image on their side until
# the GPU reaches it (unbounded -> ~5400 crops and mineru at 11-12GB, and we OOM'd a 31GB box). the GPU is compute-bound
# at ~3.3k gen tok/s and was ALREADY saturated at ~130 in flight — measured Running 130 and Running 240 produce the same
# tokens/s and the same wall time — so everything above saturation is pure cost in pinned images. sit just above it.
# nothing between here and the model may bound LOWER (see MINERU_CONN_PER_CLIENT) or the GPU starves with no trace:
# from the outside that looks exactly like a slow model
CROP_BUFFER = 12288  # crops cut and waiting on the semaphore, sized to scale with PAGES_IN_FLIGHT: if it cannot cover
# the pages, they simply queue HERE instead — a scanned page carries ~26 crops, and when this was 1024 only ~49 of the
# 512 pages in flight could hold budget while the rest blocked (budget_wait hit 115s a page on the scanned book, the
# largest single span in that run). a crop is PNG bytes by the time it is charged (~40KB, not the ~400KB of raw pixels
# it used to be), so the headroom is cheap. the semaphore still caps what is in flight AT the model; this bound only
# stops us holding crops we cannot send, and it must never be what throttles us
CROP_BOUND = CROP_CONCURRENCY + CROP_BUFFER  # hard cap on crops alive at once, across every page and every crop source
DECODE_CONCURRENCY = 16  # pages that may hold a decoded ~10MB bitmap at once — the ONLY place one ever exists, so this
# is the whole of our bitmap RAM. a page now passes through in ~0.2s (decode, build the model's 1036x1036 layout copy,
# drop the bitmap; later re-decode, cut, drop it again) because the slow part — waiting on the layout call — is spent
# OUTSIDE the gate. at a few pages a second, a 0.2s hold needs a handful of slots; 288 was sized for the old shape,
# where a page squatted here for the whole 66s layout wait. decode_wait is 0.0s on every document, so this gate is not
# contended and the ~2.2GB it was reserving is better spent on pages in flight, which is what feeds the model


@lru_cache
def get_redis() -> aioredis.Redis:
    # bounded blocking pool: a burst queues for a free connection instead of opening unbounded sockets (which starve
    # getaddrinfo on the shared executor → connect timeouts). socket_timeout=None so blocking XREADGROUP isn't cut off;
    # keepalive + health check drop dead connections; retry reconnects a blip instead of crashing the pump.
    pool = aioredis.BlockingConnectionPool.from_url(
        get_settings().redis_url,
        max_connections=REDIS_MAX_CONNECTIONS,
        timeout=None,
        socket_timeout=None,
        socket_connect_timeout=5,
        socket_keepalive=True,
        health_check_interval=30,
    )
    return aioredis.Redis(
        connection_pool=pool,
        retry=Retry(ExponentialBackoff(cap=1.0, base=0.1), 3),
        retry_on_error=[RedisConnectionError, RedisTimeoutError],
    )


_MINERU_RR = itertools.count()


@lru_cache
def _mineru_pool() -> list[MinerUClient]:
    # a fixed pool of MINERU_CLIENTS clients, each its own httpx pool capped at MINERU_CONN_PER_CLIENT connections
    # (reused). OCR jobs round-robin across them, so total sockets = clients x per-client (bounded, no fd blow-up) and
    # each pool's httpcore per-event walk is tiny (per-client^2, not total^2) instead of pegging the event loop.
    return [
        MinerUClient(
            backend="http-client",
            server_url=get_settings().mineru_base_url,
            use_tqdm=False,
            max_connections=MINERU_CONN_PER_CLIENT,
        )
        for _ in range(MINERU_CLIENTS)
    ]


def get_mineru_client() -> MinerUClient:
    return _mineru_pool()[next(_MINERU_RR) % MINERU_CLIENTS]


@lru_cache
def get_decode_gate() -> asyncio.Semaphore:
    # the only gate that admits a decoded bitmap. a page holds a slot for layout + charge + cut, then drops the image
    return asyncio.Semaphore(DECODE_CONCURRENCY)


@lru_cache
def get_crop_semaphore() -> asyncio.Semaphore:
    # ONE semaphore shared by every crop request from every page. each mineru entry point defaults to constructing a
    # fresh per-call semaphore, which bounds a single page and nothing globally — that is what let crops go unbounded
    return asyncio.Semaphore(CROP_CONCURRENCY)


@dataclass
class _CropBudget:
    # bounds crops ALIVE (cut and held in RAM), not just crops in flight: a page reserves its blocks before any crop is
    # cut, so a page whose crops cannot be absorbed yet blocks here instead of materializing images nothing will read.
    # FIFO: waiters are granted strictly in arrival order, so a 66-block page can never be starved by 17-block pages
    limit: int
    used: int = 0
    waiters: deque[tuple[int, asyncio.Future[None]]] = field(default_factory=deque)

    def free(self) -> int:
        return self.limit - self.used

    async def acquire(self, count: int) -> int:
        need = min(count, self.limit)  # a page with more blocks than the whole budget takes it all rather than deadlock
        if not self.waiters and self.free() >= need:
            self.used += need
            return need
        waiter = asyncio.get_running_loop().create_future()
        self.waiters.append((need, waiter))  # queue behind everyone already waiting — never jump the line
        await waiter
        return need

    def release(self, count: int) -> None:
        self.used -= count
        while self.waiters:
            need, waiter = self.waiters[0]
            if waiter.done():  # its page was cancelled while queued — it will never take the reservation
                self.waiters.popleft()
                continue
            if self.free() < need:
                return  # head of the line cannot fit yet; no one behind it may overtake
            self.waiters.popleft()
            self.used += need
            waiter.set_result(None)


@lru_cache
def get_crop_budget() -> _CropBudget:
    return _CropBudget(CROP_BOUND)


@lru_cache
def _thread_rapidocr(_thread_id: int) -> RapidOCR:
    # one RapidOCR per pool thread: the wrapper isn't guaranteed re-entrant, so sharing one instance across the thread
    # pool could race and garble output. keying the cache on thread id gives each worker its own engine (the fixed
    # RapidOCR pool → only a bounded few). 1 intra-op thread per engine so the pool still controls total cores.
    return RapidOCR(intra_op_num_threads=1)


def get_rapidocr() -> RapidOCR:
    return _thread_rapidocr(threading.get_ident())


@lru_cache
def get_rapidocr_pool() -> ThreadPoolExecutor:
    # bound how many scanned pages run RapidOCR at once so it can't starve the born-digital CPU stages
    return ThreadPoolExecutor(max_workers=RAPIDOCR_CONCURRENCY)


@lru_cache
def get_rapidocr_gate() -> asyncio.Semaphore:
    # one permit per pool thread: run_in_executor queues silently, so without this the queue wait is charged as OCR work
    return asyncio.Semaphore(RAPIDOCR_CONCURRENCY)


T = TypeVar("T")
_PROCESS_POOLS: list[ProcessPool] = []


@lru_cache
def get_pdfium_pool() -> ProcessPool:
    # ONE pool for every pdfium job — counting, rendering and text-layer extraction. they run the same library over the
    # same files, and a pdfium process costs hundreds of MB no matter how little it does, so three pools meant three
    # times that for no throughput: all of it together is a few hundred CPU-seconds against a GPU-bound run.
    # one process per worker (pdfium is not thread-safe; isolate by process). forkserver preloads only the lean pdf
    # module (not __main__ → no onnxruntime/cv2/mineru/xgboost in a pdfium worker). pebble kills+replaces only the
    # specific worker a per-task timeout fires on, so a hung page can't permanently shrink the pool.
    ctx = multiprocessing.get_context("forkserver")
    ctx.set_forkserver_preload(["citadel.services.pdf"])
    pool = ProcessPool(max_workers=PDFIUM_WORKERS, max_tasks=PDF_POOL_MAX_TASKS, context=ctx)
    _PROCESS_POOLS.append(pool)
    return pool


@lru_cache
def get_pdfium_gate() -> asyncio.Semaphore:
    # exactly as many permits as the pool has workers, so holding one means a worker is free. it exists to make the
    # queue wait VISIBLE: pebble queues inside schedule(), so timing the submit charges the wait to the work
    return asyncio.Semaphore(PDFIUM_WORKERS)


async def _run_pdfium[T](func: Callable[..., T], *args: object, timeout: float) -> tuple[T, float, float]:
    mark = time.time()
    async with get_pdfium_gate():
        wait = time.time() - mark
        mark = time.time()
        result = await asyncio.wrap_future(get_pdfium_pool().schedule(func, args=args, timeout=timeout))
        return result, wait, time.time() - mark


_PROFILE_DIRS: list[str] = []


def make_profile_pool(n: int) -> asyncio.Queue[str]:
    queue: asyncio.Queue[str] = asyncio.Queue()
    for _ in range(n):
        path = tempfile.mkdtemp(prefix="lo_profile_")
        _PROFILE_DIRS.append(path)  # tracked so shutdown removes exactly this process's profiles (never a shared glob)
        queue.put_nowait(path)
    return queue


def _reap_profile_dirs() -> None:
    while _PROFILE_DIRS:
        path = Path(_PROFILE_DIRS.pop())
        if path.exists():
            shutil.rmtree(path)


def release_idle() -> None:
    # ingestion is bursty but the worker is always-on, so what it warms up it then holds forever. the process pools are
    # the whole cost — several hundred MB per worker, idle between uploads — and they rebuild lazily as a fork off the
    # forkserver, in milliseconds. the RapidOCR engines are deliberately KEPT: they are only ~264MB across all threads
    # and each takes ~1.3s to reload, so dropping them would trade a rounding error for a stall on the next scan.
    while _PROCESS_POOLS:
        pool = _PROCESS_POOLS.pop()
        pool.stop()
        pool.join()
    get_pdfium_pool.cache_clear()
    release_header_pool()
    gc.collect()
    ctypes.CDLL("libc.so.6").malloc_trim(0)  # glibc keeps freed pages; without this the process footprint never drops


async def shutdown() -> None:
    # dispose every long-lived resource this process owns so termination is clean and idempotent
    while _PROCESS_POOLS:
        pool = _PROCESS_POOLS.pop()
        pool.stop()
        pool.join()
    if get_rapidocr_pool.cache_info().currsize:
        get_rapidocr_pool().shutdown(wait=False)
    _reap_profile_dirs()
    if get_redis.cache_info().currsize:
        await get_redis().aclose()


BLOB_DIR = Path(tempfile.gettempdir()) / "citadel-blobs"


async def _add_stage_seconds(doc_id: str, field: str, seconds: float) -> None:
    await get_redis().hincrbyfloat(f"doc:{doc_id}", field, seconds)


def blob_path(doc_id: str | int) -> Path:
    # doc-id-keyed source store on a shared host path (the stand-in for S3): every stage reads the source from here
    # instead of copying it through Redis, so a large PDF is memory-mapped once, never pickled per page
    return BLOB_DIR / str(doc_id)


async def reap_orphan_blobs() -> None:
    # startup: keep only blobs for docs still in flight (QUEUED/PROCESSING) so a crashed run's recovery can finish them;
    # drop terminal-doc and unknown (wiped-DB) blobs. NEVER a blanket wipe — that deletes other in-flight docs' sources
    # and hangs them forever (render/tabular find no source, the page is never recorded, no watchdog recovers it).
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


# ---- orchestrator side ----------------------------------------------------------------


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
        pipe.expire(f"doc:{doc_id}", DOC_TTL)  # safety net so a doc that never reaches merge still self-evicts
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


# ---- normalize stage (CPU / process pool) ---------------------------------------------


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


# ---- paginate stage (CPU / process pool) ----------------------------------------------


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
    # a slide is the page unit: page_idx = slide index, so content ids line up with every other paginated lane. an
    # empty deck still records one empty page, otherwise no unit job fires and the doc never reaches merge.
    data = await asyncio.to_thread(blob_path(doc_id).read_bytes)
    slides = await asyncio.to_thread(parse_pptx, data)
    await get_redis().hset(f"doc:{doc_id}", "page_count", max(len(slides), 1))
    for index, slide_blocks in enumerate(slides or [[]]):
        await record_page(doc_id, index, slide_blocks)
    logger.info("paginate file=%s pptx slides=%d", filename, len(slides))


_BLOCK_PAGINATORS = {"text": _paginate_text, "html": _paginate_html, "pptx": _paginate_pptx}


async def handle_paginate(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    kind = fields["kind"]
    redis = get_redis()
    await redis.hset(f"doc:{doc_id}", "state", "paginating")
    await redis.hsetnx(f"doc:{doc_id}", "t_paginate", time.time())
    paginator = _BLOCK_PAGINATORS.get(kind)
    if paginator is not None:
        await paginator(doc_id, fields["filename"])
        return
    if kind.startswith("image:"):
        data = await asyncio.to_thread(blob_path(doc_id).read_bytes)
        image_bytes = await asyncio.to_thread(_cap_image_bytes, data)  # cap huge scans → keep the worker's RAM bounded
        await redis.hset(f"doc:{doc_id}", "page_count", 1)
        await redis.xadd(STREAM_PAGES, {"doc_id": doc_id, "page_idx": 0, "image": image_bytes})
        logger.info("paginate file=%s image", fields["filename"])
        return
    if kind in {"xlsx", "csv", "tsv", "json"}:
        await redis.hset(f"doc:{doc_id}", "mode", "tabular")
        if kind == "xlsx":
            names = await asyncio.to_thread(sheet_names, await asyncio.to_thread(blob_path(doc_id).read_bytes))
            if not names:  # workbook with no sheets: no unit jobs would fire, so drive straight to merge
                await redis.hset(f"doc:{doc_id}", "page_count", 0)
                await redis.xadd(STREAM_MERGE, {"doc_id": doc_id})
                logger.info("paginate file=%s sheets=0 → merge", fields["filename"])
                return
            await redis.hset(f"doc:{doc_id}", "page_count", len(names))
            for sheet_no in range(1, len(names) + 1):
                await redis.xadd(STREAM_TABLES, {"doc_id": doc_id, "kind": kind, "sheet_no": sheet_no})
            logger.info("paginate file=%s sheets=%d", fields["filename"], len(names))
        else:
            await redis.hset(f"doc:{doc_id}", "page_count", 1)
            await redis.xadd(STREAM_TABLES, {"doc_id": doc_id, "kind": kind, "sheet_no": 0})
            logger.info("paginate file=%s kind=%s", fields["filename"], kind)
        return
    dpi = RENDER_DPI
    count, _, _ = await _run_pdfium(count_pdf_pages, str(blob_path(doc_id)), timeout=RENDER_TIMEOUT)
    if count <= 0:  # empty/unreadable pdf: no page units will ever be recorded, so drive the doc straight to merge
        await redis.hset(f"doc:{doc_id}", "page_count", 0)
        await get_redis().xadd(STREAM_MERGE, {"doc_id": doc_id})
        logger.info("paginate file=%s pages=0 → merge", fields["filename"])
        return
    await redis.hset(f"doc:{doc_id}", "page_count", count)
    pipe = redis.pipeline(transaction=False)
    for idx in range(count):
        pipe.xadd(STREAM_RENDER, {"doc_id": doc_id, "page_idx": idx, "dpi": dpi})
    await (
        pipe.execute()
    )  # emit one render job per page → the bounded render consumer does the work, no in-handler fan-out
    await redis.hsetnx(f"doc:{doc_id}", "t_paginated", time.time())
    logger.info("paginate file=%s pages=%d", fields["filename"], count)


async def handle_render(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    page_idx = int(fields["page_idx"])
    dpi = int(fields["dpi"])
    path = blob_path(doc_id)
    if not path.exists():
        if not await _doc_terminal(doc_id):  # source gone while the doc is still in flight → fail the page, don't hang
            msg = f"source blob missing for in-flight doc {doc_id}"
            raise FileNotFoundError(msg)
        return  # doc already finished → a reclaimed render job is a safe no-op
    redis = get_redis()
    await redis.hsetnx(f"doc:{doc_id}", "t_pages", time.time())
    (image_bytes, digital), render_wait, render_cpu = await _run_pdfium(
        render_pdf_page, str(path), page_idx, dpi, timeout=RENDER_TIMEOUT
    )
    await _add_stage_seconds(doc_id, "render_wait_s", render_wait)
    await _add_stage_seconds(doc_id, "render_s", render_cpu)
    await redis.xadd(
        STREAM_PAGES,
        {"doc_id": doc_id, "page_idx": page_idx, "image": image_bytes, "digital": "1" if digital else "0"},
    )


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
    )


# atomic unit-completion: record the unit (HSETNX dedup), and ONLY if it is new, bump done_count and — when it is the
# unit that reaches page_count — fire merge. one server-side EVAL so a crash mid-op can't wedge the doc (a redelivery
# re-runs it, HSETNX returns 0, no double-count) and merge fires exactly once even across worker restarts.
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


# requeue the bumped copy, ack the old, and delete the old as ONE server-side op, so a crash mid-retry can never leave
# both the retry copy and the still-pending original (which would double-process the job on the next recovery)
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
        keys=[f"blocks:{doc_id}", f"doc:{doc_id}", STREAM_MERGE],
        args=[str(page_idx), json.dumps([b.model_dump() for b in blocks]), doc_id, str(DOC_TTL)],
    )


async def fail_page(doc_id: str, page_idx: int) -> None:
    # one page exhausting retries must not fail the whole doc: record a marker, keep going
    await record_page(doc_id, page_idx, [Block(type="error", page_idx=page_idx, text="[extraction failed]")])


async def record_sheet(doc_id: str, sheet_no: int) -> None:
    await _record_unit()(
        keys=[f"sheets:{doc_id}", f"doc:{doc_id}", STREAM_MERGE], args=[str(sheet_no), "1", doc_id, str(DOC_TTL)]
    )


SHEETS_CACHE_MAX = 4  # workbooks kept parsed at once; every sheet of a doc reuses one parse instead of re-loading it
_SHEETS_CACHE: LRUCache[str, list[SheetExtraction]] = LRUCache(maxsize=SHEETS_CACHE_MAX)
_SHEETS_LOCK = asyncio.Lock()


async def _get_sheet(doc_id: str, sheet_no: int) -> SheetExtraction:
    async with _SHEETS_LOCK:  # serialize the load so concurrent sheets of one doc parse the workbook once
        sheets = _SHEETS_CACHE.get(doc_id)
        if sheets is None:
            data = await asyncio.to_thread(blob_path(doc_id).read_bytes)
            sheets = await asyncio.to_thread(load_all_sheets, data)
            _SHEETS_CACHE[doc_id] = sheets  # LRUCache evicts the least-recently-used workbook past maxsize
    return sheets[sheet_no - 1]


async def handle_tabular(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    kind = fields["kind"]
    sheet_no = int(fields["sheet_no"])
    redis = get_redis()
    await redis.hsetnx(f"doc:{doc_id}", "t_pages", time.time())
    path = blob_path(doc_id)
    if not path.exists():
        if not await _doc_terminal(doc_id):  # source gone while the doc is still in flight → fail, don't hang
            msg = f"source blob missing for in-flight doc {doc_id}"
            raise FileNotFoundError(msg)
        return  # doc already finished → a reclaimed sheet job is a safe no-op
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
    await cleanup(doc_id)  # terminal failure → release the source blobs (no wall-clock TTL to fall back on)


FILLIN = re.compile(r"\.{4,}|_{4,}|…")  # form fill-in markers → the line may carry handwriting the layer can't see


def _is_vlm_refusal(text: str) -> bool:
    # the VLM narrates no-text crops (QR/blank graphics) instead of staying silent ("The image provided is a QR
    # code... no textual content can be extracted"). a refusal both refers to the image AND denies content; real
    # seal/stamp/figure text does neither, so requiring both signals keeps genuine recoveries
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


CROP_SKIP = frozenset({"list", "equation_block", "image_block"})  # never cropped — mirrors prepare_for_extract
CROP_SKIP_UNANALYSED = frozenset(
    {"image", "chart"}
)  # cropped only when the client is set to analyse images (it is not)


def _reservation(client: MinerUClient, layout: ExtractResult, not_extract: list[str] | None) -> int:
    # what this page will ACTUALLY cut, not how many blocks it has. the block count is a wild over-estimate on a
    # born-digital page — ~30 blocks, of which text and title come from the pdf text layer and are never cropped, so it
    # cuts ~2 — and a page holds its reservation while blocked in the decode gate. reserving 30 to spend 2 jams that
    # gate on budget it never uses, and the gate is what feeds the VLM. this applies the same skip rules the library
    # does; it ignores only the caption/table absorption passes, which can remove more, never add. so still an upper
    # bound, and the difference is refunded once the crops are actually cut.
    skip = set(CROP_SKIP)
    if not client.helper._resolve_image_analysis(None):
        skip |= CROP_SKIP_UNANALYSED
    if not_extract:
        skip |= set(not_extract)
    return max(sum(1 for block in layout if block.type not in skip), 1)


def _layout_prompt(client: MinerUClient) -> str:
    # read the client's own [layout] prompt rather than hardcode it — we only drive the call ourselves so the page
    # bitmap can be freed before the (slow) request, not to change what is asked
    prompt: str = client.prompts.get("[layout]") or client.prompts["[default]"]
    return prompt


def _layout_params(client: MinerUClient) -> SamplingParams | None:
    params: SamplingParams | None = client.sampling_params.get("[layout]") or client.sampling_params.get("[default]")
    return params


def _encode_crops(crops: list[Image.Image | bytes]) -> list[bytes]:
    # the client PNGs every crop before sending it, so do it here instead: the raw pixel buffer dies at once and we
    # carry only the compressed bytes (~8x smaller) for however long the request waits at the model
    out: list[bytes] = []
    for crop in crops:
        if isinstance(crop, Image.Image):
            out.append(get_png_bytes(crop))
            crop.close()
        else:
            out.append(crop)
    return out


async def _predict_crop(
    client: MinerUClient,
    payload: bytes,
    prompt: str,
    param: SamplingParams | None,
    semaphore: asyncio.Semaphore,
    budget: _CropBudget,
    spans: _Spans,
) -> _PredictResult:
    # one crop, one slot: the semaphore bounds what is in flight at the model, the budget bounds what is alive in RAM,
    # and both are handed back the instant THIS crop returns — not when its slowest sibling does.
    # acquire the semaphore HERE rather than let the client do it inside _aio_predict, so the queue wait is charged to
    # crop_wait and never to predict. the client is then handed a free semaphore of its own and never blocks on it.
    mark = time.time()
    try:
        async with semaphore:
            spans.crop_wait += time.time() - mark
            mark = time.time()
            result = await client._aio_predict(payload, prompt, param, None, asyncio.Semaphore(1), None)
            spans.predict += time.time() - mark
            return result
    finally:
        budget.release(1)


@dataclass
class _Spans:
    # where a page's time inside handle_ocr actually goes. every field is either WAIT (queuing for one of our own
    # bounds) or WORK (the resource actually doing something) — never both, because a span that mixes them cannot say
    # what is slow. crop_wait/predict are summed across the page's crops, which run concurrently, so they exceed the
    # page's wall clock; they are comparable to EACH OTHER, which is the whole question: are we waiting, or is it the model
    decode_wait: float = 0.0  # queuing for a decode slot
    layout_wait: float = 0.0  # queuing for a crop slot to make the ONE layout call
    layout: float = 0.0  # that call: the VLM generating every block's box as text
    budget_wait: float = 0.0  # blocked on crop budget, holding nothing but the encoded page
    cut: float = 0.0  # PIL: re-decode, cut the crops out, PNG them (cpu)
    crop_wait: float = 0.0  # summed over crops: queuing for a crop slot
    predict: float = 0.0  # summed over crops: the VLM actually reading them


SPAN_FIELDS = ("decode_wait", "layout_wait", "layout", "budget_wait", "cut", "crop_wait", "predict")


async def _record_spans(doc_id: str, spans: _Spans) -> None:
    for field_name in SPAN_FIELDS:
        await _add_stage_seconds(doc_id, f"{field_name}_s", getattr(spans, field_name))


async def extract_page(client: MinerUClient, doc_id: str, image: bytes, not_extract: list[str] | None) -> ExtractResult:
    # a page's ~10MB decoded bitmap is needed to lay out and to cut crops — never to wait on the predictions, which is
    # the long part. so it lives only inside the decode gate: lay out, charge the budget, cut, drop the bitmap, leave.
    # the page then waits out the VLM holding just its crops and the ~2MB of encoded bytes it already had.
    # crops are charged BEFORE they are cut, so none can exist uncharged — cutting first and charging after would let a
    # scanned page (~27 crops) multiply through the gate and rebuild the very explosion the budget exists to stop.
    # a page blocked here has ALREADY laid out, so a freed crop slot is refilled by a cut (~50ms), not by a fresh
    # render+layout round trip (~0.6s): the pages waiting at this gate are the reservoir that keeps the VLM fed
    budget = get_crop_budget()
    semaphore = get_crop_semaphore()
    spans = _Spans()

    # LAYOUT. the model resizes the page to 1036x1036 for this anyway, so the moment that copy exists our ~10MB bitmap
    # is dead weight — and the call itself is slow (it generates every block's box as text: 66s a page, measured, and
    # up to 266 blocks on a dense one). holding a decode slot across that wait is what made pages queue for the gate:
    # 288 slots each pinned for a minute by a page doing nothing but waiting on the network.
    mark = time.time()
    async with get_decode_gate():
        spans.decode_wait = time.time() - mark
        with Image.open(io.BytesIO(image)) as img:
            layout_image = await client.helper.aio_prepare_for_layout(client.executor, img)
    mark = time.time()  # bitmap gone, slot released — we hold only the small layout copy for the long call
    async with semaphore:  # a layout call is one crop slot, and its queue wait is its own span, not the call's
        spans.layout_wait = time.time() - mark
        mark = time.time()
        output = await client._aio_predict(
            layout_image, _layout_prompt(client), _layout_params(client), None, asyncio.Semaphore(1), None
        )
        spans.layout = time.time() - mark
    del layout_image
    layout = ExtractResult(await client.helper.aio_parse_layout_output(client.executor, output.text), output.scored)

    # CUT. re-decode (~50ms) rather than carry the bitmap across the layout wait, and turn every crop into its encoded
    # bytes here: the client would PNG them anyway, so encoding now lets the raw pixel buffers die immediately and
    # leaves us holding only the compressed form (~8x smaller) until the response lands.
    mark = time.time()
    granted = await budget.acquire(_reservation(client, layout, not_extract))
    spans.budget_wait = time.time() - mark
    mark = time.time()
    async with get_decode_gate():
        with Image.open(io.BytesIO(image)) as img:
            try:
                crops, prompts, params, indices = await client.helper.aio_prepare_for_extract(
                    client.executor, img, layout, not_extract, None
                )
            except BaseException:
                budget.release(granted)  # the crops never existed, so no _predict_crop will hand this back
                raise
        payloads = await asyncio.to_thread(_encode_crops, crops)
    spans.cut = time.time() - mark
    if len(payloads) > granted:  # the absorption passes only ever remove, so this cannot happen — charge it if it does
        granted += await budget.acquire(len(payloads) - granted)
    budget.release(granted - len(payloads))  # refund the estimate's slack. the budget now holds exactly len(payloads),
    # and from here each crop hands its own slot back — the page keeps no lump reservation of its own
    # drive the crops individually rather than through aio_batch_predict, which gathers them and returns only when the
    # SLOWEST comes back. the budget is a reservation on crops ALIVE, so releasing it in one lump at the end means a
    # page squats on all 26 slots long after 25 of them are done — tail latency holding the whole reservation. a
    # 26-crop scanned page then stalls every page behind it in the decode gate (253s per page against 85s unbounded).
    # each crop now returns its slot and drops its image the moment its own request lands.
    outputs = await asyncio.gather(
        *(
            _predict_crop(client, payload, prompt, param, semaphore, budget, spans)
            for payload, prompt, param in zip(payloads, prompts, params, strict=True)
        )
    )
    for idx, output in zip(indices, outputs, strict=True):
        layout[idx].content = output.text
        layout[idx].scored = output.scored
    processed = await client.helper.aio_post_process(client.executor, layout)
    await _record_spans(doc_id, spans)
    return ExtractResult(processed, layout.layout_scored)


def _cut(img: Image.Image, blocks: list[ContentBlock]) -> list[Image.Image]:
    width, height = img.size
    return [
        img.crop((int(b.bbox[0] * width), int(b.bbox[1] * height), int(b.bbox[2] * width), int(b.bbox[3] * height)))
        for b in blocks
    ]


async def _vlm_recover(client: MinerUClient, image: bytes, blocks: list[ContentBlock]) -> None:
    # crop each block's region and OCR it as text via the VLM. same shape as extract_page: decode inside the page
    # buffer, cut, drop the bitmap, and only then queue the crops against the global budget — recovery crops are VLM
    # work too, so they cannot be allowed to exceed it either
    if not blocks:
        return
    budget = get_crop_budget()
    async with get_decode_gate():
        with Image.open(io.BytesIO(image)) as img:
            granted = await budget.acquire(len(blocks))  # charged before cutting — see extract_page
            crops = _cut(img, blocks)
    try:
        recovered = await client.aio_batch_content_extract(crops, types="text", semaphore=get_crop_semaphore())
        for block, text in zip(blocks, recovered, strict=True):
            clean = str(text or "").strip()
            if clean and not _is_vlm_refusal(clean):
                block.content = clean
    finally:
        for crop in crops:
            crop.close()
        budget.release(granted)


@lru_cache
def _qr_detector() -> cv2.QRCodeDetector:
    return cv2.QRCodeDetector()


def _qr_payload(crop: Image.Image) -> str | None:
    # a QR/barcode has no prose; the VLM would narrate it ("...no textual content can be extracted"). detect it
    # deterministically: None = not a QR (let the VLM read seal/stamp text), else the decoded payload ("" if unreadable)
    text, points, _ = _qr_detector().detectAndDecode(np.asarray(crop.convert("RGB")))
    return text if points is not None else None


def _qr_scan(img: Image.Image, content_blocks: ExtractResult) -> list[ContentBlock]:
    width, height = img.size
    to_ocr: list[ContentBlock] = []
    for cb in content_blocks:
        if cb.type != "image" or (cb.content or "").strip():
            continue
        b = cb.bbox
        crop = img.crop((int(b[0] * width), int(b[1] * height), int(b[2] * width), int(b[3] * height)))
        try:
            payload = _qr_payload(crop)
        finally:
            crop.close()
        if payload is None:
            to_ocr.append(cb)  # not a QR → VLM-crop reads the seal/stamp/figure text
        elif payload:
            cb.content = payload  # decoded QR → store the real encoded data instead of a hallucinated description
    return to_ocr


async def ocr_empty_blocks(client: MinerUClient, image: bytes, content_blocks: ExtractResult) -> int:
    # image blocks the VLM localized but left empty (seals, stamps, figures, logos) → OCR the crop as text.
    # the VLM never read these (it only localizes images), so this is a fresh request, not a failed retry.
    # the QR scan needs the bitmap, so it runs inside the page buffer and hands back only the blocks to re-read
    async with get_decode_gate():
        with Image.open(io.BytesIO(image)) as img:
            to_ocr = _qr_scan(img, content_blocks)
    await _vlm_recover(client, image, to_ocr)  # re-opens under the buffer to cut; never holds a bitmap across the VLM
    return len(to_ocr)


async def recover_fillin_blocks(client: MinerUClient, image: bytes, content_blocks: ExtractResult) -> int:
    # text/title blocks that are empty or fill-in fields may hold ink the layer / full-page VLM missed → re-OCR a
    # focused crop of just that block. the crop fills the VLM frame, giving the region far higher effective
    # resolution than the downscaled full page, so it can read a stamp/word the first pass dropped
    flagged = [
        cb
        for cb in content_blocks
        if cb.type in LAYER_TYPES and (not (cb.content or "").strip() or FILLIN.search(cb.content or ""))
    ]
    await _vlm_recover(client, image, flagged)
    return len(flagged)


RAPIDOCR_MIN_SCORE = 0.85  # drop low-confidence recognitions — usually garbled re-reads of decorative/stamp text
CROP_LOG_THRESHOLD = 6  # log a page only when its empty+fill-in VLM crops exceed this — surfaces burners, no spam


def _trigrams(text: str) -> set[str]:
    s = re.sub(r"\s+", "", text).lower()
    return {s[i : i + 3] for i in range(len(s) - 2)}


def _scanned_gap_lines(
    page: np.ndarray, covered: list[list[float]], text_blocks: list[tuple[list[float], str]]
) -> list[tuple[list[float], str]]:
    # det every visual line, rec it, and keep ONLY confident lines that the VLM didn't already produce:
    # not inside a table/image block, and their text not already in the VLM text block overlapping the line
    engine = get_rapidocr()
    height, width = page.shape[:2]
    boxes, _ = engine(page, use_det=True, use_cls=False, use_rec=False)
    out: list[tuple[list[float], str]] = []
    for box in boxes or []:
        x0, x1 = min(p[0] for p in box) / width, max(p[0] for p in box) / width
        y0, y1 = min(p[1] for p in box) / height, max(p[1] for p in box) / height
        if x1 <= x0 or y1 <= y0:
            continue
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in covered):
            continue
        result, _ = engine(page[int(y0 * height) : int(y1 * height), int(x0 * width) : int(x1 * width)], use_det=False)
        if not result or float(result[0][1]) < RAPIDOCR_MIN_SCORE:
            continue
        tris = _trigrams(result[0][0])
        dup = any(
            len(tris & _trigrams(content)) >= 0.4 * len(tris)
            for bbox, content in text_blocks
            if bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3]
        )
        if not tris or dup:
            continue
        out.append(([x0, y0, x1, y1], result[0][0]))
    return out


async def recover_scanned_gaps(img: Image.Image, blocks: list[Block], page_idx: int) -> tuple[float, float]:
    # scanned page: the VLM already produced the (good) text; RapidOCR (CPU, bounded) adds only the lines it dropped.
    # runs in the decoupled gapfill stage, so it never holds the GPU OCR slot or pins a decoded page array under it.
    # the gate has one permit per pool thread, so the wait for a thread is measured here instead of inside the work
    # BGR, not RGB: RapidOCR only swaps channels for path/bytes/PIL inputs — an ndarray it takes as already-BGR
    # (its LoadImage.convert_img returns it untouched), so handing it RGB silently transposes red and blue
    page = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    covered = [list(b.bbox) for b in blocks if b.type in {"table", "image"}]
    text_blocks = [(list(b.bbox), b.text or "") for b in blocks if b.type in LAYER_TYPES]
    loop = asyncio.get_running_loop()
    mark = time.time()
    async with get_rapidocr_gate():
        wait = time.time() - mark
        mark = time.time()
        gaps = await loop.run_in_executor(get_rapidocr_pool(), _scanned_gap_lines, page, covered, text_blocks)
        work = time.time() - mark
    for bbox, text in gaps:
        blocks.append(Block(type="text", page_idx=page_idx, bbox=bbox, text=text))
    return wait, work


async def handle_ocr(fields: dict[str, str], image: bytes) -> None:
    doc_id = fields["doc_id"]
    page_idx = int(fields["page_idx"])
    digital = fields.get("digital") == "1"
    await get_redis().hincrby(f"doc:{doc_id}", "started_count", 1)
    client = get_mineru_client()  # one of the pooled clients, round-robin — its httpx pool is reused, not per-page
    ocr_t = time.time()
    if digital:
        # born-digital page: VLM does layout + recognizes only non-text; text/title come from the exact text layer
        content_blocks = await extract_page(client, doc_id, image, list(LAYER_TYPES))
        text_blocks = [cb for cb in content_blocks if cb.type in LAYER_TYPES]
        path = blob_path(doc_id)
        if text_blocks and path.exists():
            # awaited on the OCR path: every digital page waits here, so it gates the VLM's feed
            layer, layer_wait, layer_cpu = await _run_pdfium(
                extract_layer_by_bbox,
                str(path),
                page_idx,
                [list(cb.bbox) for cb in text_blocks],
                timeout=RENDER_TIMEOUT,
            )
            await _add_stage_seconds(doc_id, "layer_wait_s", layer_wait)
            await _add_stage_seconds(doc_id, "layer_s", layer_cpu)
            for cb, text in zip(text_blocks, layer, strict=True):
                if text:
                    cb.content = text
        # digital only: the VLM never read these text blocks (we used the layer), so a focused crop is a fresh attempt
        # that can catch handwriting the layer lacks. on scanned pages the VLM already read them, so a re-crop only
        # risks re-introducing the same drop and slightly degrading the text — skip it there.
        fill_crops = await recover_fillin_blocks(client, image, content_blocks)
    else:
        content_blocks = await extract_page(client, doc_id, image, None)  # VLM reads the scanned text (primary)
        fill_crops = 0
    img_crops = await ocr_empty_blocks(client, image, content_blocks)  # empty image blocks → VLM-crop
    await _add_stage_seconds(doc_id, "ocr_s", time.time() - ocr_t)
    blocks = [map_content_block(cb, page_idx) for cb in content_blocks]
    if img_crops + fill_crops >= CROP_LOG_THRESHOLD:  # surface only crop-heavy pages — no per-page spam over thousands
        logger.info(
            "ocr-heavy doc=%s page=%d digital=%d blocks=%d img_crops=%d fill_crops=%d",
            doc_id,
            page_idx,
            int(digital),
            len(blocks),
            img_crops,
            fill_crops,
        )
    if not digital and GAP_FILL:
        # hand the page to the bounded CPU gapfill stage and free the GPU OCR slot now, instead of blocking on RapidOCR
        await get_redis().xadd(
            STREAM_GAPFILL, {"doc_id": doc_id, "page_idx": page_idx, "image": image, "blocks": _dump_blocks(blocks)}
        )
        return
    await _emit_page(doc_id, page_idx, blocks)


def _dump_blocks(blocks: list[Block]) -> str:
    return json.dumps([b.model_dump() for b in blocks])


def _load_blocks(blob: str) -> list[Block]:
    return [Block(**raw) for raw in json.loads(blob)]


async def _emit_page(doc_id: str, page_idx: int, blocks: list[Block]) -> None:
    await record_page(doc_id, page_idx, blocks)


async def handle_gapfill(fields: dict[str, str], image: bytes) -> None:
    # decoupled CPU stage: RapidOCR adds the lines the VLM dropped on a scanned page, then the page is finalized
    page_idx = int(fields["page_idx"])
    blocks = _load_blocks(fields["blocks"])
    with Image.open(io.BytesIO(image)) as img:
        gapfill_wait, gapfill_cpu = await recover_scanned_gaps(img, blocks, page_idx)
    await _add_stage_seconds(fields["doc_id"], "gapfill_wait_s", gapfill_wait)
    await _add_stage_seconds(fields["doc_id"], "gapfill_s", gapfill_cpu)
    await _emit_page(fields["doc_id"], page_idx, blocks)


async def emit_vlm_only(fields: dict[str, str]) -> None:
    # gapfill exhausted its retries → finalize with the VLM blocks alone; a RapidOCR error must never drop the page
    await _emit_page(fields["doc_id"], int(fields["page_idx"]), _load_blocks(fields["blocks"]))


# ---- merge stage ----------------------------------------------------------------------


STAGE_SECONDS = (  # accumulated per-page, in pipeline order. every *_wait is US making the page queue on one of our own
    # bounds; every other field is the resource actually working. they are separated because a span that mixes the two
    # cannot answer the only question worth asking — is the pipeline slow, or is the model
    ("render_wait", "render_wait_s"),
    ("render_cpu", "render_s"),
    ("ocr_wall", "ocr_s"),  # the page's elapsed time in handle_ocr; the fields below decompose it
    ("decode_wait", "decode_wait_s"),
    ("layout_wait", "layout_wait_s"),
    ("layout", "layout_s"),
    ("budget_wait", "budget_wait_s"),
    ("cut", "cut_s"),
    ("crop_wait", "crop_wait_s"),
    ("predict", "predict_s"),
    ("layer_wait", "layer_wait_s"),
    ("layer_cpu", "layer_s"),
    ("gapfill_wait", "gapfill_wait_s"),
    ("gapfill_cpu", "gapfill_s"),
)


def _stage_line(doc: dict[bytes, bytes]) -> str:
    # render/ocr wall time is meaningless: render is backpressured on the pages stream, so its span is GPU queue wait,
    # not work. report the ACTUAL cpu/gpu seconds each stage spent (accumulated per page) plus the queue wait separately
    def g(key: str) -> float:
        return float(doc.get(key.encode(), 0) or 0)

    proc, paginate, paginated = g("t_proc"), g("t_paginate"), g("t_paginated")
    pages, merge, done = g("t_pages"), g("t_merge"), g("t_done")
    head = (("normalize", proc, paginate), ("paginate", paginate, paginated), ("queued", paginated, pages))
    tail = (("pages_wall", pages, merge), ("merge", merge, done))
    walls = [f"{name}={end - start:.1f}s" for name, start, end in head if start and end]
    walls += [f"{name}={g(key):.1f}s" for name, key in STAGE_SECONDS if g(key)]
    walls += [f"{name}={end - start:.1f}s" for name, start, end in tail if start and end]
    return " ".join(walls)


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
    per_page = await redis.hgetall(f"blocks:{doc_id}")
    blocks: list[Block] = []
    for page_idx in sorted(int(k) for k in per_page):
        blocks.extend(Block(**raw) for raw in json.loads(per_page[str(page_idx).encode()]))
    source = (await redis.hget(f"doc:{doc_id}", "filename") or b"").decode()
    state = DocumentStatus.PARTIAL if any(b.type == "error" for b in blocks) else DocumentStatus.INGESTED
    await save_document_tree(int(doc_id), blocks, state)
    await persist_document_tree(int(doc_id))
    await redis.hset(f"doc:{doc_id}", mapping={"state": state, "t_done": time.time()})
    doc = await redis.hgetall(f"doc:{doc_id}")
    t0 = float(doc.get(b"t0", 0) or 0)
    logger.info(
        "merge file=%s state=%s blocks=%d dur=%.1fs %s", source, state, len(blocks), time.time() - t0, _stage_line(doc)
    )


async def cleanup(doc_id: str) -> None:
    redis = get_redis()
    _SHEETS_CACHE.pop(doc_id, None)  # doc finished → drop its parsed workbook
    await redis.delete(f"blocks:{doc_id}", f"sheets:{doc_id}")
    blob_path(doc_id).unlink(missing_ok=True)  # the doc's source file is freed the moment it finishes
    await redis.expire(f"doc:{doc_id}", 3600)  # keep final status briefly, then auto-evict — no accumulation


# ---- stage wiring (used by the worker entrypoint) -------------------------------------


async def ensure_group(stream: str) -> None:
    try:
        await get_redis().xgroup_create(stream, GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise

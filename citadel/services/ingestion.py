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
from citadel.tabular.structure import structure_tables
from citadel.utils import normalize_file
from config import CPU_EIGHTH, CPU_THIRD

if TYPE_CHECKING:
    from citadel.tabular.materialize import MaterializedTable

logger = logging.getLogger(__name__)

GROUP = "citadel"
STREAM_INGEST = "ingest"
STREAM_NORMALIZED = "normalized"
RENDER_DOCS = "render:docs"  # documents with render jobs outstanding — the rotation the render consumer reads over.
# render jobs are per-DOCUMENT streams, read with a share PROPORTIONAL TO REMAINING WORK. all three orderings have now
# been measured on this corpus:
#   FIFO            472.8s — the dominant doc's jobs sit behind everyone's, so sparse docs run alone for 70s and the
#                            model collapses to 1-5 crops in flight while every queue reads empty
#   round-robin     478.0s — equal share gave the critical path 1/N ~ 5%, fixing the stall and stretching that doc 27%
#   proportional      this — both finish together, which is where makespan is minimised
# EQUAL SHARE IS THE BUG and must never be the fallback, including on the first pass before any density is known —
# there the weight comes from PAGE COUNT, which already ranks the dominant document first
STREAM_PAGES = "pages"
STREAM_STRUCTURE = "structure"  # ungated: prepare + route each doc as its ocr completes (no-table docs go on to merge)
STREAM_TABLE_STRUCTURE = "table_structure"  # THE table stream: every table unit from every source — spreadsheet
# sheets and table-bearing pdf/html documents alike — is structured here, in one stage, after ocr has drained
STREAM_MERGE = "merge"


def render_stream(doc_id: str) -> str:
    return f"render:{doc_id}"


MAX_ATTEMPTS = 3
DOC_TTL = 86_400  # safety expiry on doc/blocks/sheets keys: set at submit, refreshed on every recorded unit, so a doc
# that somehow never reaches merge (and so never hits cleanup) still self-evicts instead of accumulating in Redis
# forever. far longer than any single doc's processing, so it never evicts live state; cleanup shortens it on finish.
RENDER_DPI = 150  # validated equal to 200 and ~26% faster
# the RAMP, and the one place a cpu bound is worth spending: until a document is paginated the gpu has no work from it
# AT ALL, so these are not "feeding more than enough" — they are the serialization in front of an idle model. measured:
# 36 files through 3 slots left the 809-page book, which carries 68% of all model work, unpaginated for 10.8s of a
# ~510s run. cpu stays deliberately under-used everywhere downstream, where supply already outruns the gpu
PAGINATE_CONCURRENCY = CPU_THIRD
RENDER_CONCURRENCY = CPU_EIGHTH
PDFIUM_WORKERS = 4  # every pdfium job — counting, rendering AND text-layer extraction — shares these, and the text
# layer is the one that matters: it is awaited INSIDE the ocr path, so a page waiting for a pdfium worker is a page not
# feeding the model. one worker was right when layout was a 20s generation and pdfium had all the time in the world;
# with a detector doing layout in 95ms, digital pages arrive at the text layer immediately and one worker cannot keep
# up — measured layer_wait=262s against layer_cpu=8.9s, i.e. 97% queueing. each worker stands at ~650MB, which is real,
# but host RAM is no longer the scarce thing it was (the OCR model's 11GB and the gap-fill engines are both gone)
RENDER_TIMEOUT = 120  # page renders in <1s; if a job strands (dead/hung pool worker) free the slot + retry the page
PDF_POOL_MAX_TASKS = 100  # recycle a pdfium worker every N pages: a long-lived one grows to hundreds of MB and never
# gives it back. respawn is a fork off the (lean) forkserver, so the cost is milliseconds amortised over 100 pages,
# against a stage that is already the cheapest in the pipeline
DETECT_WORKERS = 1  # ONE detector process. more is not just unnecessary, it is WORSE: two contend for the card and
# measured SLOWER than one (22.5 pages/s at one, 19.5 at two, 11.7 at two under load), while each carries its own
# cuda context and model — the second cost ~1.7GB of VRAM to make the detector slower. demand is ~4 pages/s.
# batching is pointless too (44ms/page at batch 1, 42ms at batch 16): the cost is CPU-side, not GPU
UNCHARGED_PAGES = 512  # pages claimed but not yet charged to the crop budget — the ONE window a page is invisible to
# every other bound, between its claim and the detector finding its regions. it is NOT a limit on pages in flight: a
# charged page is already counted in crops and must not be counted again. sized generously because a page sits here
# only for a detector pass (~100ms), and because a crop-sparse document needs many pages resident to put 128 crops in
# front of the model at all — measured 0.82 crops/page on a born-digital book against 23.5 on a scanned one, so any
# fixed page count is right for one of them and starves the other. host RAM bounds it: ~2MB of encoded image each
CROP_BUFFER = 12288  # crops cut and waiting on the semaphore. if it cannot cover the pages in flight they simply queue
# HERE instead, and budget_wait — not the model — becomes what limits us. a crop is PNG bytes by the time it is charged
# (~40KB, not the ~400KB of raw pixels), so the headroom is cheap. the semaphore still caps what is in flight AT the
# model; this bound only stops us holding crops we cannot send, and it must never be what throttles us
CROP_BOUND = CROP_CONCURRENCY + CROP_BUFFER  # hard cap on crops alive at once, across every page and every crop source
DECODE_CONCURRENCY = 16  # pages that may hold a decoded ~10MB bitmap at once — the ONLY place one ever exists, so this
# is the whole of our bitmap RAM. a page now passes through in ~0.2s (decode, build the model's 1036x1036 layout copy,
# drop the bitmap; later re-decode, cut, drop it again) because the slow part — waiting on the layout call — is spent
# OUTSIDE the gate. at a few pages a second, a 0.2s hold needs a handful of slots; 288 was sized for the old shape,
# where a page squatted here for the whole 66s layout wait. decode_wait is 0.0s on every document, so this gate is not
# contended and the ~2.2GB it was reserving is better spent on pages in flight, which is what feeds the model


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


@dataclass
class _Admission:
    # pages CLAIMED but not yet accounted for in crops. this is the only window a page is unmeasurable: the crop budget
    # cannot see a page until the detector has found its regions, so between the claim and that charge the page holds an
    # encoded image the budget reads as free. bound THAT window — not the page's whole life. a page that has charged is
    # already represented in the crop budget, and counting it twice is what starved the model: at a fixed 96 pages, a
    # document yielding 0.8 crops a page could put only ~79 crops in front of 128 slots and could not claim more,
    # because its pages sat holding slots while their crops queued. crops are the unit the GPU consumes; pages are not
    limit: int
    pending: set[tuple[str, int]] = field(default_factory=set)

    def free(self) -> int:
        return self.limit - len(self.pending)

    def enter(self, doc_id: str, page_idx: int) -> None:
        self.pending.add((doc_id, page_idx))

    def settle(self, doc_id: str, page_idx: int) -> None:
        self.pending.discard((doc_id, page_idx))  # idempotent: charged normally, or released by the job's finally


TIMELINE_BUCKET = 10.0  # seconds per reported point
TIMELINE_TICK = 1.0
STARVED = CROP_CONCURRENCY // 2  # below half the slots, the model is demonstrably not being fed


@dataclass
class _Timeline:
    # WHERE the idle sits, not how much of it there is. occupancy gives the total; only a time series says whether it
    # is the ramp, the tail, or a stall in between — and the crops ALIVE at the same instant say which: slots empty
    # while crops are queued would be a dispatch fault, slots empty with nothing queued is starvation upstream
    inflight: int = 0
    started: float = 0.0
    buckets: list[list[float]] = field(default_factory=list)  # [inflight_sum, alive_sum, samples]

    def enter(self) -> None:
        self.inflight += 1
        if not self.started:
            self.started = time.time()  # the clock starts at the FIRST crop, so the series is not padded by boot

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


@lru_cache
def get_detect_pool() -> ProcessPoolExecutor:
    # layout is a DETECTOR: one RT-DETR forward pass per page, boxes and reading order out.
    #
    # PROCESSES, not threads, and not a fork. its cost is CPU-side python — batching it changes nothing (44ms/page at
    # batch 1, 42ms at batch 16) — so in-process threads do not scale it: measured 22.5 pages/s on one process against
    # 20.9 on two threads, LESS than one, because they fight over the GIL. worse, they fight the event loop that drives
    # every crop request in flight, which is what stalls it (layout_wait ran to 5-10s a page) and what starves the GPU.
    # fork is out: a cuda context cannot survive it, and python's ONE forkserver has a GLOBAL preload list that the
    # pdfium, tabular and detect pools would all be fighting over. so: spawn, one fresh interpreter per worker, each
    # initialising cuda for itself.
    # stdlib, not pebble: pebble SWALLOWS a child that dies at startup and reports only "the pool is not active", which
    # is why the first attempt at this was undebuggable. ProcessPoolExecutor propagates the child's exception.
    return ProcessPoolExecutor(max_workers=DETECT_WORKERS, mp_context=multiprocessing.get_context("spawn"))


@lru_cache
def get_detect_gate() -> asyncio.Semaphore:
    # one permit per thread: run_in_executor queues silently, so without this the queue wait is charged as detector work
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
    # forkserver, in milliseconds. the detector is NOT dropped: it is a thread in this process holding a cuda context
    # and ~400MB of VRAM, and tearing that down would mean re-initialising cuda on the next upload for nothing.
    while _PROCESS_POOLS:
        pool = _PROCESS_POOLS.pop()
        pool.stop()
        pool.join()
    get_pdfium_pool.cache_clear()
    gc.collect()
    ctypes.CDLL("libc.so.6").malloc_trim(0)  # glibc keeps freed pages; without this the process footprint never drops


async def shutdown() -> None:
    # dispose every long-lived resource this process owns so termination is clean and idempotent
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
    await redis.hset(f"doc:{doc_id}", "kind", kind)  # read back at table_structure to route OCR tables to the model
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
    if count <= 0:  # empty/unreadable pdf: no page units will ever be recorded, so drive the doc straight to merge
        await redis.hset(f"doc:{doc_id}", "page_count", 0)
        await get_redis().xadd(STREAM_MERGE, {"doc_id": doc_id})
        logger.info("paginate file=%s pages=0 → merge", fields["filename"])
        return
    await redis.hset(f"doc:{doc_id}", "page_count", count)
    stream = render_stream(doc_id)
    pipe = redis.pipeline(transaction=False)
    for idx in range(count):
        pipe.xadd(stream, {"doc_id": doc_id, "page_idx": idx, "dpi": dpi})
    await pipe.execute()  # emit one render job per page → the bounded render consumer does the work
    # joins the rotation only AFTER its jobs exist, so "in the set with an empty stream" means drained, never pending
    await redis.sadd(RENDER_DOCS, doc_id)
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
        render_pdf_page, str(path), page_idx, dpi, job_timeout=RENDER_TIMEOUT
    )
    await _add_stage_seconds(doc_id, "render_wait_s", render_wait)
    await _add_stage_seconds(doc_id, "render_s", render_cpu)
    await redis.xadd(
        STREAM_PAGES,
        {"doc_id": doc_id, "page_idx": page_idx, "image": image_bytes, "digital": "1" if digital else "0"},
    )


# ---- ocr stage: detector (pool) finds the regions, the VLM (vLLM) reads each crop ------------------


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


async def render_weights(docs: list[str]) -> dict[str, float]:
    # REMAINING CROPS per document, which is what the share must be proportional to. density is observed
    # (crops seen / pages finished) and refines as the document runs; a document that has finished no pages yet has no
    # density of its own and borrows the corpus mean, so its weight is still denominated in crops and comparable.
    # the fallback chain never reaches "all equal" — that is the ordering that measured worst
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
        # floored at ONE crop per page: a fully-digital document owes no crops at all (its text comes from the layer),
        # and weighting purely by crops would give it no share and never render its pages — it would simply never
        # finish. every remaining page costs a render and a detect whatever its crop yield, so it always carries weight
        weights[doc_id] = max(1.0, (pages - done) * max(density, 1.0))
    return weights


async def record_page(doc_id: str, page_idx: int, blocks: list[Block]) -> None:
    # the last page fires STRUCTURE (not merge): a doc with OCR blocks may hold tables, and those are structured in the
    # structure stage before merge ever runs. tabular sheets go straight to merge — they are already structured upstream
    await _record_unit()(
        keys=[f"blocks:{doc_id}", f"doc:{doc_id}", STREAM_STRUCTURE],
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
    # a sheet is a unit of work exactly as a page is, and it marks itself started for the same reason: `started_count`
    # minus `done_count` is what progress reports as ACTIVE. only the page path used to bump it, so a spreadsheet
    # reported nothing in flight and sat at "queued" until the whole document flipped to ingested — it never showed as
    # processing, however long its sheets took
    await redis.hincrby(f"doc:{doc_id}", "started_count", 1)
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


@dataclass
class _Spans:
    # where a page's time inside handle_ocr actually goes. every field is either a WAIT (queuing on one of our own
    # bounds) or WORK (the resource actually doing something), never both — a span that mixes them cannot say what is
    # slow. these are ACCUMULATORS: pages run concurrently and so do the crops within a page, so the raw sums overlap
    # and are not durations of anything. they are divided by their unit count before they are ever reported.
    decode_wait: float = 0.0  # queuing for a decode slot
    layout_wait: float = 0.0  # queuing for a detector thread
    layout: float = 0.0  # the detector: ONE forward pass. no generation, so it cannot loop or return an empty page
    budget_wait: float = 0.0  # blocked on crop budget, holding nothing but the encoded page
    cut: float = 0.0  # PIL: re-decode, cut the crops out, resize them into the model's pixel window, PNG them (cpu)
    crop_wait: float = 0.0  # over the page's crops: queuing for a crop slot
    predict: float = 0.0  # over the page's crops: the model actually reading them
    crops: int = 0  # how many crops those two are summed over — without it the sums mean nothing


SPAN_FIELDS = ("decode_wait", "layout_wait", "layout", "budget_wait", "cut", "crop_wait", "predict")


async def _record_spans(doc_id: str, spans: _Spans) -> None:
    for field_name in SPAN_FIELDS:
        await _add_stage_seconds(doc_id, f"{field_name}_s", getattr(spans, field_name))
    await _add_stage_seconds(doc_id, "crops_n", float(spans.crops))


def reading_order(blocks: list[DetBlock]) -> list[DetBlock]:
    # the detector's pointer network orders the CONTENT regions; page furniture (headers, pictures) comes back
    # unordered. keep its order for what it ordered, and slot everything else in by where it sits on the page
    ordered = sorted((b for b in blocks if b.order is not None), key=lambda b: b.order or 0)
    loose = sorted((b for b in blocks if b.order is None), key=lambda b: (b.bbox[1], b.bbox[0]))
    out = list(ordered)
    for block in loose:
        at = next((i for i, other in enumerate(out) if other.bbox[1] > block.bbox[1]), len(out))
        out.insert(at, block)
    return out


def _to_read(blocks: list[DetBlock], digital: bool) -> list[int]:
    # which regions the model is asked to read. a picture has no text. and on a born-digital page the prose already
    # exists as real characters in the PDF, so re-recognizing it could only introduce errors — we take it from the layer
    # instead, which is why a digital page costs ~2 crops where a scanned one costs ~26
    skip = LAYER_LABELS if digital else frozenset()
    return [i for i, b in enumerate(blocks) if is_readable(b) and b.label not in skip]


GAP_GRID = 24  # the page is rasterized into this many cells per axis to approximate the coverage complement — coarse
# enough to be cheap, fine enough that a genuinely missed table does not vanish into one giant cell
GAP_CELL_PX = 6  # each cell is represented by this many px/side in the downsampled image the content signal reads
# from — resizing DOWN first and computing std on the small image, rather than on the full page, is what keeps this
# cheap: measured 11ms/page this way against 108ms computing the same signal at full resolution
GAP_MIN_AREA_FRACTION = 0.04  # a gap below this is a margin/gutter, not a candidate miss
GAP_CONTENT_STD = 2.0  # grayscale std of the downsampled uncovered cells above this reads as real content rather
# than blank background. calibrated against exactly one known miss (Citadel Pitch Deck.pdf slide 9: content_std=3.7)
# and one true-blank slide (content_std=0.7) — real separation, but one example each, not a measured corpus. THIS IS
# LOG-ONLY: it never triggers a crop or a model call, it exists to make misses visible so a real threshold can be
# set from real data instead of guessed twice


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


# regions that may be READ TOGETHER. every crop costs the model's 144-token floor whether it fills it or not, so two
# adjacent regions read as one cost half. only prose qualifies: a run of paragraphs concatenated is still the same
# prose, whereas merging two headings would fuse two titles, two display formulas would return one LaTeX blob for two
# equations, and a list is already whole. measured on the 809-page scanned book — 12,908 regions become 9,938 crops
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


COLUMN_WIDEN = 1.25  # a merge may grow the block DOWNWARD, never sideways


def _stacked(group: list[float], bbox: list[float]) -> bool:
    # regions in one column sit above one another: merging them leaves the width alone and only adds height. two
    # regions SIDE BY SIDE are a different matter — their union is a wide short strip holding two independent columns,
    # and reading it as one crop both scrambles the order and costs accuracy. measured on a bilingual letterhead: the
    # Kazakh and Russian addresses merged into one 89%-wide block and the model returned "данфылы" for "даңғылы",
    # which the reference pipeline read correctly by keeping the two columns apart. area alone cannot catch this —
    # two short columns have a SMALL union area — so the test is on width: a merge must not widen the block
    union = max(group[2], bbox[2]) - min(group[0], bbox[0])
    widest = max(group[2] - group[0], bbox[2] - bbox[0])
    return union <= widest * COLUMN_WIDEN


@dataclass
class _GroupCloses:
    # WHY a group ended, which decides where packing can go next. adjacent merging is at 23% and prose runs average
    # 1.57 — if groups mostly close on AREA the union bbox is the limit (two stacked lines carry the dead whitespace
    # between them, so composing the crop tightly instead of taking their union would pack far more). if they mostly
    # close on LABEL then adjacent merging is exhausted and the remaining headroom needs non-adjacent regions, which
    # means re-associating returned text with its source regions. very different projects; this says which
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
    # consecutive same-label prose regions share a crop while their UNION still fits the model's floor. past the floor
    # a merge stops being free — the bill grows with the union — so the group closes and a new one starts
    groups: list[list[int]] = []
    unions: list[list[float]] = []
    for index in indices:
        label = blocks[index].label
        bbox = list(blocks[index].bbox)
        # index + 1, not merely "the previous readable region": a picture sitting between two paragraphs is not in
        # `indices`, and merging across it would place its content after text that follows it on the page
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
    # cut, fit the model's pixel window, and encode — all while the bitmap is alive, so the raw pixels die here and only
    # the compressed form (~8x smaller) waits out the queue. PNG and NOT base64: base64 is 33% larger and every crop
    # alive would carry that, which is host RAM spent to save an encode that was never on the critical path
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
    # one crop, one slot: the semaphore bounds what is in flight at the model, the budget bounds what is alive in RAM,
    # and both are handed back the instant THIS crop returns — not when its slowest sibling does. the semaphore is
    # acquired HERE so the queue wait is charged to crop_wait and never to predict
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
    # a page's ~10MB decoded bitmap is needed only to CUT — never to wait on the model, which is the long part. so it
    # lives inside the decode gate: cut, drop the bitmap, leave. the page then waits out the model holding just its
    # crops and the ~2MB of encoded bytes it already had.
    # crops are charged BEFORE they are cut, so none can exist uncharged — cutting first and charging after would let a
    # scanned page (~26 crops) multiply through the gate and rebuild the very explosion the budget exists to stop.
    budget = get_crop_budget()
    semaphore = get_crop_semaphore()
    spans = _Spans()

    # LAYOUT. a detector forward pass in its own pool: it takes no crop slot, generates no tokens, and cannot fail the
    # way a language model laying out a page can (loop on itself, or emit nothing at all for a whole page)
    blocks, spans.layout_wait, spans.layout = await _run_detect(image)
    blocks = reading_order(blocks)
    indices = _to_read(blocks, digital)

    # CUT.
    mark = time.time()
    granted = await budget.acquire(max(len(indices), 1))
    # charged: this page is now represented in crops, so it stops occupying an admission slot and the next page can be
    # claimed. a sparse page frees its slot having added almost nothing, so admission keeps pulling until crops fill
    get_admission().settle(doc_id, page_idx)
    spans.budget_wait = time.time() - mark
    mark = time.time()
    async with get_decode_gate():
        spans.decode_wait = 0.0
        with Image.open(io.BytesIO(image)) as img:
            groups = group_crops(blocks, indices, img.size)
            gap = await asyncio.to_thread(_coverage_gap, img, blocks)
            if gap is not None:
                fraction, content = gap
                logger.info(
                    "coverage gap doc=%s page=%d uncovered=%.0f%% content_std=%.1f — log-only, no rescue",
                    doc_id,
                    page_idx,
                    fraction * 100,
                    content,
                )
            try:
                payloads = await asyncio.to_thread(_cut_crops, img, blocks, groups)
            except BaseException:
                budget.release(granted)  # the crops never existed, so no _read_crop will hand this back
                raise
    spans.cut = time.time() - mark
    spans.crops = len(payloads)
    budget.release(granted - len(payloads))  # refund the estimate's slack; from here each crop hands its own slot back

    texts = await asyncio.gather(
        *(
            _read_crop(payload, prompt_for(blocks[group[0]].label), semaphore, budget, spans)
            for group, payload in zip(groups, payloads, strict=True)
        )
    )
    await _record_spans(doc_id, spans)
    return _blocks_from_groups(blocks, groups, texts)


def _blocks_from_groups(blocks: list[DetBlock], groups: list[list[int]], texts: list[str]) -> list[Block]:
    # a merged group leaves ONE block, carrying the union of its regions and the single text the model returned for
    # them. the regions folded in are not emitted — their content is in that text, and a second empty block for each
    # would be a node with no content. order is untouched: a group is contiguous, so its head sits where it always did
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
                page_idx=0,  # set by the caller, which knows the page
                bbox=group_bbox(blocks, group),
                text=block_text(block.label, text),
            )
        )
    return out


async def _reread(image: bytes, blocks: list[Block]) -> None:
    # crop each block's region and read it again, on its own. same shape as extract_page: cut inside the decode gate,
    # drop the bitmap, and only then queue the crops against the global budget — a re-read is model work too, so it
    # cannot be allowed to exceed it either
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
                    [[index] for index in range(len(blocks))],  # a re-read targets one block at a time; never merged
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
    # a QR/barcode has no prose; the model would narrate it ("...no textual content can be extracted"). detect it
    # deterministically: None = not a QR (let the model read the seal/stamp text), else the decoded payload
    # ("" when it is a QR but unreadable)
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
            block.text = payload  # decoded QR → the real encoded data, which no OCR of the glyph could recover
            decoded += 1
    return decoded


async def read_pictures(image: bytes, blocks: list[Block]) -> int:
    # a picture region is DECODED, never read. the detector already tells us what a region is, and it has a class for
    # every picture that carries text — `seal` and `chart` are their own labels, read on the normal path with their own
    # task prompts. what is left under `image` is a photograph or a figure: there is no text on it to recognize.
    # asking the model to read one anyway is what the reference pipeline calls use_ocr_for_image_block, and it defaults
    # it to FALSE. we had it on — inherited from MinerU, whose layout vocabulary could not tell a seal from a figure —
    # and it is where the runaway generations came from: given nothing to read, the model repeats a fragment until it
    # exhausts its token budget, and thousands of tokens of that landed in the index.
    # a QR is different: it is data, not text, and cv2 decodes it exactly with no model at all.
    async with get_decode_gate():
        with Image.open(io.BytesIO(image)) as img:
            return _qr_scan(img, blocks)


async def recover_fillin_blocks(image: bytes, blocks: list[Block]) -> int:
    # text blocks that came from the PDF's text layer but are empty or are fill-in fields may hold ink the layer cannot
    # see — a signature, a handwritten date. re-read a focused crop of just that block: the crop fills the model's
    # frame, giving the region far higher effective resolution than it had as part of a whole page
    flagged = [
        b for b in blocks if b.type in LAYER_LABELS and (not (b.text or "").strip() or FILLIN.search(b.text or ""))
    ]
    await _reread(image, flagged)
    return len(flagged)


CROP_LOG_THRESHOLD = 6  # log a page only when its re-read crops exceed this — surfaces burners, no spam over thousands


async def _rescue_uncovered(doc_id: str, path: Path, page_idx: int, blocks: list[Block]) -> tuple[list[Block], int]:
    # the detector misses text — on a form it boxes the STRUCTURE and not the filled-in VALUES, and we measured what
    # that costs: 20% of borang_13's characters and 14% of defence's, never boxed and so never read. lowering the score
    # threshold recovers some of it and starts reading OTHER regions twice, which is a worse trade.
    # on a born-digital page there is nothing to trade. the text layer says exactly which characters no box covers, so
    # they are added as their own regions: no model call, no duplication, and the characters are the document's own.
    # coverage on a page with a text layer becomes 1.0 by construction rather than by tuning.
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
    # slot each rescued run into the EXISTING order by where it sits on the page — never re-sort the whole page. the
    # detector's pointer network decided that order and it is the one thing here that understands columns; a positional
    # sort would silently replace it with top-to-bottom and shred any two-column page.
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
        # born-digital page: the detector found the regions, and the prose comes from the page's OWN characters at each
        # box — not re-recognized, so it cannot be misread
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
        # rare and worth seeing every time: text the detector never boxed, taken from the page's own characters
        logger.info("rescued doc=%s page=%d runs=%d — text no region covered", doc_id, page_idx, rescued)
    if img_crops + fill_crops >= CROP_LOG_THRESHOLD:
        logger.info(
            "ocr-heavy doc=%s page=%d digital=%d blocks=%d img_crops=%d fill_crops=%d",
            doc_id,
            page_idx,
            int(digital),
            len(blocks),
            img_crops,
            fill_crops,
        )
    await _emit_page(doc_id, page_idx, blocks)


async def _emit_page(doc_id: str, page_idx: int, blocks: list[Block]) -> None:
    await record_page(doc_id, page_idx, blocks)


# ---- merge stage ----------------------------------------------------------------------


STAGE_SECONDS = (  # (label, redis key, unit). every *_wait is US making something queue on one of our own bounds; every
    # other field is the resource actually working. they are kept apart because a span that mixes them cannot answer the
    # only question worth asking — is the pipeline slow, or is the model.
    # the UNIT is not decoration. these are accumulators over units that run CONCURRENTLY — many pages at once, and many
    # crops at once within a page — so the raw sum is not a duration of anything and comparing it to wall time is
    # meaningless. each is reported as a MEAN over the unit it was summed across, which is a real number you can reason
    # about: "the model spends 0.4s on a crop" and "a crop waits 1.2s for a slot" are comparable; 688 and 195 are not.
    ("render_wait", "render_wait_s", "pg"),
    ("render_cpu", "render_s", "pg"),
    ("ocr_wall", "ocr_s", "pg"),  # a page's elapsed time in handle_ocr; the fields below decompose it
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
    # NEVER print a raw accumulator. pages run concurrently and so do the crops inside a page, so every sum here is a
    # sum of OVERLAPPING spans — it is not a duration, and set next to wall time it is nonsense. divide each by the
    # units it was summed over and report the mean, which is a number that means something on its own.
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
    walls += [f"{name}={end - start:.1f}s" for name, start, end in tail if start and end]
    return " ".join(walls)


async def _read_doc_blocks(doc_id: str) -> list[Block]:
    per_page = await get_redis().hgetall(f"blocks:{doc_id}")
    blocks: list[Block] = []
    for page_idx in sorted(int(k) for k in per_page):
        blocks.extend(Block(**raw) for raw in json.loads(per_page[str(page_idx).encode()]))
    return blocks


async def handle_structure(fields: dict[str, str]) -> None:
    # the moment a doc's ocr completes, prepare (cpu) and ROUTE. no table blocks → straight to merge, so the doc ingests
    # during ocr; has tables → publish ONE JOB PER TABLE BLOCK onto the table stream. the count is recorded first, so a
    # job that finishes before the rest are queued cannot see a complete set and fire merge early
    doc_id = fields["doc_id"]
    redis = get_redis()
    blocks = await _read_doc_blocks(doc_id)
    prepared = prepare_document(blocks)
    await redis.set(f"structures:{doc_id}", dump_structures(prepared), ex=DOC_TTL)
    indices = table_block_indices(prepared.stitched)
    if not indices:
        await redis.xadd(STREAM_MERGE, {"doc_id": doc_id})
        return
    await redis.xadd(STREAM_TABLE_STRUCTURE, {"doc_id": doc_id, "unit": "document"})


async def handle_table_structure(fields: dict[str, str]) -> None:
    # THE table structure extraction stage — the single place a table, from ANY source, is structured. every unit is a
    # JOB ON THE STREAM, claimed under the stage's capacity: a SHEET (spreadsheet / csv / json, table data end to end)
    # or a TABLE BLOCK (one grid a pdf / html doc yielded). redelivery of either is a no-op
    if fields["unit"] == "sheet":
        await handle_tabular(fields)
        return
    doc_id = fields["doc_id"]
    redis = get_redis()
    prepared = load_structures(await redis.get(f"structures:{doc_id}"))
    indices = table_block_indices(prepared.stitched)
    grids = [grid_from_html(prepared.stitched[index].text or "") for index in indices]
    structured = await structure_tables(grids)  # every candidate at once → structure + drop non-tables + merge splits
    by_block: dict[int, list[MaterializedTable]] = {}
    for table, blocks in structured:  # each table lands on its FIRST source block; the rest yield nothing
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
    _SHEETS_CACHE.pop(doc_id, None)  # doc finished → drop its parsed workbook
    await redis.delete(f"blocks:{doc_id}", f"sheets:{doc_id}", f"structures:{doc_id}", f"tables:{doc_id}")
    await redis.srem(RENDER_DOCS, doc_id)
    await redis.delete(render_stream(doc_id))
    blob_path(doc_id).unlink(missing_ok=True)  # the doc's source file is freed the moment it finishes
    await redis.expire(f"doc:{doc_id}", 3600)  # keep final status briefly, then auto-evict — no accumulation


# ---- stage wiring (used by the worker entrypoint) -------------------------------------


async def ensure_group(stream: str) -> None:
    try:
        await get_redis().xgroup_create(stream, GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise

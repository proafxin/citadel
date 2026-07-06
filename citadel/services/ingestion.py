import asyncio
import io
import itertools
import json
import logging
import multiprocessing
import re
import shutil
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
import redis.asyncio as aioredis
from fastapi import HTTPException, UploadFile
from mineru_vl_utils import MinerUClient
from mineru_vl_utils.structs import ContentBlock, ExtractResult
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
from citadel.services.excel import extract_sheet_no, extract_tables, sheet_names
from citadel.services.html import parse_html
from citadel.services.library import library_exists
from citadel.services.pdf import MAX_IMAGE_SIDE, count_pdf_pages, downscale, extract_layer_by_bbox, render_pdf_page
from citadel.services.tabular import extract_json_tables, structure_csv_tables
from citadel.utils import normalize_file
from config import CPU_QUARTER, CPU_THIRD, get_settings

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
LAYER_TYPES = ("text", "title")  # filled from the PDF text layer on born-digital pages (skip VLM recognition)

MINERU_CLIENTS = (
    16  # pool of VLM clients; OCR jobs round-robin across them (more, smaller pools → cheaper event-loop walk)
)
MINERU_CONN_PER_CLIENT = 16  # sockets per client (reused). clients x per-client = 256 total = OCR_CONCURRENCY, bounded
REDIS_MAX_CONNECTIONS = 64  # bounded blocking pool: callers queue for a connection, never open unbounded sockets
RENDER_DPI = 150  # validated equal to 200 (the VLM resizes internally) and ~26% faster
DIGITAL_RENDER_DPI = 110  # office→pdf ONLY (provably born-digital): image is layout-only, text from the PDF layer → render small. validate layout still holds; regular pdf stays at RENDER_DPI
PAGINATE_CONCURRENCY = CPU_QUARTER  # pdfium process-pool workers, one dedicated pdfium per process
PAGINATE_RECYCLE = 64
RENDER_CONCURRENCY = CPU_QUARTER  # in-flight render jobs; matches the pdfium pool width so pages never queue in RAM
RAPIDOCR_CONCURRENCY = CPU_THIRD  # scanned-page gap-OCR threads (CPU); bounds RapidOCR so it can't starve
GAP_FILL = True  # RapidOCR scanned gap-fill; set to False for clean-image benchmarks (pure VLM)


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
def get_rapidocr() -> RapidOCR:
    # CPU PP-OCR; 1 intra-op thread per call so a bounded pool controls total cores (no oversubscription)
    return RapidOCR(intra_op_num_threads=1)


@lru_cache
def get_rapidocr_pool() -> ThreadPoolExecutor:
    # bound how many scanned pages run RapidOCR at once so it can't starve the born-digital CPU stages
    return ThreadPoolExecutor(max_workers=RAPIDOCR_CONCURRENCY)


@lru_cache
def get_paginate_pool() -> ProcessPoolExecutor:
    # one process per worker → each gets its own pdfium (pdfium is not thread-safe; isolate by process).
    # forkserver preloads only the lean pdf module (not __main__ → no onnxruntime/cv2/mineru/xgboost in a render
    # worker); max_tasks_per_child recycles each worker so pdfium's process-global font cache can't grow unbounded.
    ctx = multiprocessing.get_context("forkserver")
    ctx.set_forkserver_preload(["citadel.services.pdf"])
    return ProcessPoolExecutor(
        max_workers=PAGINATE_CONCURRENCY,
        mp_context=ctx,
        max_tasks_per_child=PAGINATE_RECYCLE,
    )


def make_profile_pool(n: int) -> asyncio.Queue[str]:
    queue: asyncio.Queue[str] = asyncio.Queue()
    for _ in range(n):
        queue.put_nowait(tempfile.mkdtemp(prefix="lo_profile_"))
    return queue


BLOB_DIR = Path(tempfile.gettempdir()) / "citadel-blobs"


def blob_path(doc_id: str | int) -> Path:
    # doc-id-keyed source store on a shared host path (the stand-in for S3): every stage reads the source from here
    # instead of copying it through Redis, so a large PDF is memory-mapped once, never pickled per page
    return BLOB_DIR / str(doc_id)


def reset_blob_dir() -> None:
    if BLOB_DIR.exists():
        shutil.rmtree(BLOB_DIR)
    BLOB_DIR.mkdir(parents=True, exist_ok=True)


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
        done, total, state = await redis.hmget(f"doc:{doc_id}", "done_count", "page_count", "state")
        progress.append(
            DocProgress(
                doc_id=doc_id,
                filename=filename,
                done=int(done) if done else 0,
                total=int(total) if total else 0,
                state=state.decode() if state else "",
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
    img = Image.open(io.BytesIO(image_bytes))
    if max(img.size) <= MAX_IMAGE_SIDE:
        return image_bytes
    out = io.BytesIO()
    downscale(img).save(out, format="PNG")
    return out.getvalue()


async def handle_paginate(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    kind = fields["kind"]
    redis = get_redis()
    await redis.hset(f"doc:{doc_id}", "state", "paginating")
    await redis.hsetnx(f"doc:{doc_id}", "t_paginate", time.time())
    if kind == "text":
        data = await asyncio.to_thread(blob_path(doc_id).read_bytes)
        text = data.decode("utf-8", errors="replace")
        blocks = [
            Block(type="text", page_idx=0, text=part.strip()) for part in re.split(r"\n\s*\n", text) if part.strip()
        ]
        await redis.hset(f"doc:{doc_id}", "page_count", 1)
        await record_page(doc_id, 0, blocks)
        logger.info("paginate file=%s text paragraphs=%d", fields["filename"], len(blocks))
        return
    if kind == "html":
        data = await asyncio.to_thread(blob_path(doc_id).read_bytes)
        blocks = await asyncio.to_thread(parse_html, data)
        await redis.hset(f"doc:{doc_id}", "page_count", 1)
        await record_page(doc_id, 0, blocks)
        logger.info("paginate file=%s html blocks=%d", fields["filename"], len(blocks))
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
            await redis.hset(f"doc:{doc_id}", "page_count", len(names))
            for sheet_no in range(1, len(names) + 1):
                await redis.xadd(STREAM_TABLES, {"doc_id": doc_id, "kind": kind, "sheet_no": sheet_no})
            logger.info("paginate file=%s sheets=%d", fields["filename"], len(names))
        else:
            await redis.hset(f"doc:{doc_id}", "page_count", 1)
            await redis.xadd(STREAM_TABLES, {"doc_id": doc_id, "kind": kind, "sheet_no": 0})
            logger.info("paginate file=%s kind=%s", fields["filename"], kind)
        return
    dpi = DIGITAL_RENDER_DPI if kind == "office-pdf" else RENDER_DPI
    loop = asyncio.get_running_loop()
    count = await loop.run_in_executor(get_paginate_pool(), count_pdf_pages, str(blob_path(doc_id)))
    await redis.hset(f"doc:{doc_id}", "page_count", count)
    pipe = redis.pipeline(transaction=False)
    for idx in range(count):
        pipe.xadd(STREAM_RENDER, {"doc_id": doc_id, "page_idx": idx, "dpi": dpi})
    await (
        pipe.execute()
    )  # emit one render job per page → the bounded render consumer does the work, no in-handler fan-out
    logger.info("paginate file=%s pages=%d", fields["filename"], count)


async def handle_render(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    page_idx = int(fields["page_idx"])
    dpi = int(fields["dpi"])
    path = blob_path(doc_id)
    if not path.exists():  # source already cleaned up (doc finished) → a reclaimed render job is a no-op
        return
    redis = get_redis()
    await redis.hsetnx(f"doc:{doc_id}", "t_pages", time.time())
    loop = asyncio.get_running_loop()
    image_bytes, digital = await loop.run_in_executor(get_paginate_pool(), render_pdf_page, str(path), page_idx, dpi)
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


async def record_page(doc_id: str, page_idx: int, blocks: list[Block]) -> None:
    await _record_unit()(
        keys=[f"blocks:{doc_id}", f"doc:{doc_id}", STREAM_MERGE],
        args=[str(page_idx), json.dumps([b.model_dump() for b in blocks]), doc_id],
    )


async def fail_page(doc_id: str, page_idx: int) -> None:
    # one page exhausting retries must not fail the whole doc: record a marker, keep going
    await record_page(doc_id, page_idx, [Block(type="error", page_idx=page_idx, text="[extraction failed]")])


async def record_sheet(doc_id: str, sheet_no: int) -> None:
    await _record_unit()(keys=[f"sheets:{doc_id}", f"doc:{doc_id}", STREAM_MERGE], args=[str(sheet_no), "1", doc_id])


async def handle_tabular(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    kind = fields["kind"]
    sheet_no = int(fields["sheet_no"])
    redis = get_redis()
    await redis.hsetnx(f"doc:{doc_id}", "t_pages", time.time())
    path = blob_path(doc_id)
    if not path.exists():  # source already cleaned up (doc finished) → a reclaimed sheet job is a no-op
        return
    data = await asyncio.to_thread(path.read_bytes)
    if kind == "xlsx":
        sheet = await asyncio.to_thread(extract_sheet_no, data, sheet_no)
        tables = await extract_tables(sheet)
        sheet_name = sheet.sheet_name
    else:
        filename = (await redis.hget(f"doc:{doc_id}", "filename") or b"").decode()
        if kind == "json":
            tables = await asyncio.to_thread(extract_json_tables, data, filename.rsplit(".", 1)[0] or "root")
        else:
            separator = "\t" if kind == "tsv" else ","
            tables = list(enumerate(await structure_csv_tables(data, separator), start=1))
        sheet_name = filename
    await save_sheet_tables(int(doc_id), sheet_no, sheet_name, tables)
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


async def _vlm_recover(client: MinerUClient, img: Image.Image, blocks: list[ContentBlock]) -> None:
    # crop each block's region and OCR it as text via the VLM; per page, concurrent across pages → vLLM batches it
    if not blocks:
        return
    width, height = img.size
    crops = [
        img.crop((int(b.bbox[0] * width), int(b.bbox[1] * height), int(b.bbox[2] * width), int(b.bbox[3] * height)))
        for b in blocks
    ]
    recovered = await client.aio_batch_content_extract(crops, types="text")
    for block, text in zip(blocks, recovered, strict=True):
        clean = str(text or "").strip()
        if clean and not _is_vlm_refusal(clean):
            block.content = clean


def _qr_payload(crop: Image.Image) -> str | None:
    # a QR/barcode has no prose; the VLM would narrate it ("...no textual content can be extracted"). detect it
    # deterministically: None = not a QR (let the VLM read seal/stamp text), else the decoded payload ("" if unreadable)
    text, points, _ = cv2.QRCodeDetector().detectAndDecode(np.asarray(crop.convert("RGB")))
    return text if points is not None else None


async def ocr_empty_blocks(client: MinerUClient, img: Image.Image, content_blocks: ExtractResult) -> None:
    # image blocks the VLM localized but left empty (seals, stamps, figures, logos) → OCR the crop as text.
    # the VLM never read these (it only localizes images), so this is a fresh request, not a failed retry
    width, height = img.size
    to_ocr: list[ContentBlock] = []
    for cb in content_blocks:
        if cb.type != "image" or (cb.content or "").strip():
            continue
        b = cb.bbox
        payload = _qr_payload(img.crop((int(b[0] * width), int(b[1] * height), int(b[2] * width), int(b[3] * height))))
        if payload is None:
            to_ocr.append(cb)  # not a QR → VLM-crop reads the seal/stamp/figure text
        elif payload:
            cb.content = payload  # decoded QR → store the real encoded data instead of a hallucinated description
    await _vlm_recover(client, img, to_ocr)


async def recover_fillin_blocks(client: MinerUClient, img: Image.Image, content_blocks: ExtractResult) -> None:
    # text/title blocks that are empty or fill-in fields may hold ink the layer / full-page VLM missed → re-OCR a
    # focused crop of just that block. the crop fills the VLM frame, giving the region far higher effective
    # resolution than the downscaled full page, so it can read a stamp/word the first pass dropped
    flagged = [
        cb
        for cb in content_blocks
        if cb.type in LAYER_TYPES and (not (cb.content or "").strip() or FILLIN.search(cb.content or ""))
    ]
    await _vlm_recover(client, img, flagged)


RAPIDOCR_MIN_SCORE = 0.85  # drop low-confidence recognitions — usually garbled re-reads of decorative/stamp text


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


async def recover_scanned_gaps(img: Image.Image, blocks: list[Block], page_idx: int) -> None:
    # scanned page: the VLM already produced the (good) text; RapidOCR (CPU, bounded) adds only the lines it dropped.
    # runs in the decoupled gapfill stage, so it never holds the GPU OCR slot or pins a decoded page array under it
    page = np.asarray(img.convert("RGB"))
    covered = [list(b.bbox) for b in blocks if b.type in {"table", "image"}]
    text_blocks = [(list(b.bbox), b.text or "") for b in blocks if b.type in LAYER_TYPES]
    loop = asyncio.get_running_loop()
    gaps = await loop.run_in_executor(get_rapidocr_pool(), _scanned_gap_lines, page, covered, text_blocks)
    for bbox, text in gaps:
        blocks.append(Block(type="text", page_idx=page_idx, bbox=bbox, text=text))


async def handle_ocr(fields: dict[str, str], image: bytes) -> None:
    doc_id = fields["doc_id"]
    page_idx = int(fields["page_idx"])
    digital = fields.get("digital") == "1"
    img = Image.open(io.BytesIO(image))
    client = get_mineru_client()  # one of the pooled clients, round-robin — its httpx pool is reused, not per-page
    if digital:
        # born-digital page: VLM does layout + recognizes only non-text; text/title come from the exact text layer
        content_blocks = await client.aio_two_step_extract(img, not_extract_list=list(LAYER_TYPES))
        text_blocks = [cb for cb in content_blocks if cb.type in LAYER_TYPES]
        path = blob_path(doc_id)
        if text_blocks and path.exists():
            loop = asyncio.get_running_loop()
            layer = await loop.run_in_executor(
                get_paginate_pool(),
                extract_layer_by_bbox,
                str(path),
                page_idx,
                [list(cb.bbox) for cb in text_blocks],
            )
            for cb, text in zip(text_blocks, layer, strict=True):
                if text:
                    cb.content = text
        # digital only: the VLM never read these text blocks (we used the layer), so a focused crop is a fresh
        # attempt that can catch handwriting the layer lacks. on scanned pages the VLM already read them, so a
        # re-crop only risks re-introducing the same drop and slightly degrading the text — skip it there.
        await recover_fillin_blocks(client, img, content_blocks)
    else:
        content_blocks = await client.aio_two_step_extract(img)  # VLM reads the scanned text (primary, good quality)
    await ocr_empty_blocks(client, img, content_blocks)  # image blocks (seals/stamps/figures) → VLM-crop, both branches
    blocks = [map_content_block(cb, page_idx) for cb in content_blocks]
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
    await recover_scanned_gaps(Image.open(io.BytesIO(image)), blocks, page_idx)
    await _emit_page(fields["doc_id"], page_idx, blocks)


async def emit_vlm_only(fields: dict[str, str]) -> None:
    # gapfill exhausted its retries → finalize with the VLM blocks alone; a RapidOCR error must never drop the page
    await _emit_page(fields["doc_id"], int(fields["page_idx"]), _load_blocks(fields["blocks"]))


# ---- merge stage ----------------------------------------------------------------------


def _stage_line(doc: dict[bytes, bytes]) -> str:
    def g(key: str) -> float:
        return float(doc.get(key.encode(), 0) or 0)

    proc, paginate, pages, merge, done = g("t_proc"), g("t_paginate"), g("t_pages"), g("t_merge"), g("t_done")
    walls: list[str] = []
    if proc and paginate:
        walls.append(f"normalize={paginate - proc:.1f}s")
    if paginate and pages:
        walls.append(f"paginate={pages - paginate:.1f}s")
    if pages and merge:
        walls.append(f"pages={merge - pages:.1f}s")
    if merge and done:
        walls.append(f"merge={done - merge:.1f}s")
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
    await redis.delete(f"blocks:{doc_id}", f"sheets:{doc_id}")
    blob_path(doc_id).unlink(missing_ok=True)  # the doc's source file is freed the moment it finishes
    await redis.expire(f"doc:{doc_id}", 3600)  # keep final status briefly, then auto-evict — no accumulation


# ---- stage wiring (used by the worker entrypoint) -------------------------------------

STREAMS = (STREAM_INGEST, STREAM_NORMALIZED, STREAM_RENDER, STREAM_PAGES, STREAM_GAPFILL, STREAM_MERGE, STREAM_TABLES)


async def ensure_group(stream: str) -> None:
    try:
        await get_redis().xgroup_create(stream, GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise

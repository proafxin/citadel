import asyncio
import io
import json
import logging
import re
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from functools import lru_cache

import cv2
import numpy as np
import pypdfium2 as pdfium
import redis.asyncio as aioredis
from fastapi import HTTPException, UploadFile
from mineru_vl_utils import MinerUClient
from mineru_vl_utils.structs import ContentBlock, ExtractResult
from PIL import Image
from rapidocr_onnxruntime import RapidOCR
from redis.exceptions import ResponseError

from citadel.llm import describe_table
from citadel.schemas.content import Block
from citadel.schemas.document import DocumentStatus, IngestResponse
from citadel.services.document import (
    create_document,
    finalize_tabular,
    persist_document_tree,
    save_document_tree,
    save_sheet_tables,
)
from citadel.services.excel import extract_sheet_no, extract_tables, sheet_names
from citadel.services.html import parse_html
from citadel.services.library import library_exists
from citadel.services.tabular import extract_json_tables, read_csv_table
from citadel.utils import normalize_file
from config import get_settings

logger = logging.getLogger(__name__)

GROUP = "citadel"
STREAM_INGEST = "ingest"
STREAM_NORMALIZED = "normalized"
STREAM_PAGES = "pages"
STREAM_MERGE = "merge"
STREAM_TABLES = "tables"
STREAM_GAPFILL = "gapfill"  # decoupled CPU stage: scanned pages do RapidOCR gap-fill here, off the GPU OCR slot


MAX_ATTEMPTS = 3
LAYER_TYPES = ("text", "title")  # filled from the PDF text layer on born-digital pages (skip VLM recognition)


@lru_cache
def get_redis() -> aioredis.Redis:
    # socket_timeout=None so the blocking XREADGROUP isn't cut off by a client read timeout
    return aioredis.from_url(get_settings().redis_url, socket_timeout=None)


@lru_cache
def get_mineru_client() -> MinerUClient:
    # max_connections caps the shared httpx pool so concurrent VLM requests queue for a connection instead of
    # opening unbounded sockets (the per-block fan-out × page concurrency otherwise exhausts the fd table)
    return MinerUClient(
        backend="http-client",
        server_url=get_settings().mineru_base_url,
        use_tqdm=False,
        max_connections=get_settings().mineru_max_connections,
    )


@lru_cache
def get_rapidocr() -> RapidOCR:
    # CPU PP-OCR; 1 intra-op thread per call so a bounded pool controls total cores (no oversubscription)
    return RapidOCR(intra_op_num_threads=1)


@lru_cache
def get_rapidocr_pool() -> ThreadPoolExecutor:
    # bound how many scanned pages run RapidOCR at once so it can't starve the born-digital CPU stages
    return ThreadPoolExecutor(max_workers=get_settings().rapidocr_concurrency)


@lru_cache
def get_paginate_pool() -> ProcessPoolExecutor:
    # one process per worker → each gets its own pdfium (pdfium is not thread-safe; isolate by process)
    return ProcessPoolExecutor(max_workers=get_settings().paginate_concurrency)


def make_profile_pool(n: int) -> asyncio.Queue[str]:
    queue: asyncio.Queue[str] = asyncio.Queue()
    for _ in range(n):
        queue.put_nowait(tempfile.mkdtemp(prefix="lo_profile_"))
    return queue


# ---- orchestrator side ----------------------------------------------------------------


async def submit_document(data: bytes, filename: str, library_id: int) -> int:
    doc_id = await create_document(library_id, filename)
    redis = get_redis()
    await redis.hset(
        f"doc:{doc_id}",
        mapping={"state": "queued", "filename": filename, "done_count": 0, "t0": time.time()},
    )
    await redis.xadd(STREAM_INGEST, {"doc_id": doc_id, "filename": filename, "data": data})
    logger.info("ingest file=%s doc_id=%s", filename, doc_id)
    return doc_id


async def submit_documents(files: list[UploadFile], library_id: int) -> IngestResponse:
    if not await library_exists(library_id):
        raise HTTPException(status_code=404, detail="unknown library")
    doc_ids = [await submit_document(await file.read(), file.filename or "upload", library_id) for file in files]
    return IngestResponse(doc_ids=doc_ids)


async def get_status(doc_id: int) -> DocumentStatus:
    raw = await get_redis().hgetall(f"doc:{doc_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="unknown doc_id")
    data = {key.decode(): value.decode() for key, value in raw.items()}
    return DocumentStatus(
        state=data.get("state", ""),
        filename=data.get("filename"),
        page_count=int(data["page_count"]) if "page_count" in data else None,
        done_count=int(data["done_count"]) if "done_count" in data else None,
    )


# ---- normalize stage (CPU / process pool) ---------------------------------------------


async def handle_normalize(fields: dict[str, str], profile_dir: str, data: bytes) -> None:
    doc_id = fields["doc_id"]
    await get_redis().hset(f"doc:{doc_id}", "state", "normalizing")
    kind, normalized = await asyncio.to_thread(normalize_file, data, fields["filename"], profile_dir)
    await get_redis().xadd(
        STREAM_NORMALIZED, {"doc_id": doc_id, "kind": kind, "filename": fields["filename"], "data": normalized}
    )
    logger.info("normalize file=%s kind=%s", fields["filename"], kind)


# ---- paginate stage (CPU / process pool) ----------------------------------------------


# cap a page image's long side → bounds the worker's resident RAM (it holds full-res decoded pages to crop them).
# the VLM resizes internally so the full-page pass is unaffected; kept generous so seal/stamp crops stay legible
MAX_IMAGE_SIDE = 2500


def _downscale(img: Image.Image) -> Image.Image:
    if max(img.size) <= MAX_IMAGE_SIDE:
        return img
    scale = MAX_IMAGE_SIDE / max(img.size)
    return img.resize((round(img.width * scale), round(img.height * scale)))


def _cap_image_bytes(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes))
    if max(img.size) <= MAX_IMAGE_SIDE:
        return image_bytes
    out = io.BytesIO()
    _downscale(img).save(out, format="PNG")
    return out.getvalue()


def render_pdf_pages(pdf_bytes: bytes, dpi: int) -> list[tuple[bytes, bool]]:
    # (png, is_digital) per page; is_digital = has a real text layer and isn't rotated → safe to read by bbox
    pages: list[tuple[bytes, bool]] = []
    pdf = pdfium.PdfDocument(pdf_bytes)
    scale = dpi / 72
    for page in pdf:
        bio = io.BytesIO()
        _downscale(page.render(scale=scale).to_pil()).save(bio, format="PNG")
        digital = page.get_rotation() == 0 and page.get_textpage().count_chars() > 16
        pages.append((bio.getvalue(), digital))
    pdf.close()
    return pages


def extract_layer_by_bbox(pdf_bytes: bytes, page_idx: int, bboxes: list[list[float]]) -> list[str]:
    # exact text from the PDF text layer inside each normalized (0-1, top-left) bbox
    pdf = pdfium.PdfDocument(pdf_bytes)
    page = pdf[page_idx]
    width, height = page.get_size()
    textpage = page.get_textpage()
    out: list[str] = []
    for x0, y0, x1, y1 in bboxes:
        left, right, bottom, top = x0 * width, x1 * width, (1 - y1) * height, (1 - y0) * height
        out.append(textpage.get_text_bounded(left=left, bottom=bottom, right=right, top=top).strip())
    pdf.close()
    return out


async def handle_paginate(fields: dict[str, str], data: bytes) -> None:
    doc_id = fields["doc_id"]
    kind = fields["kind"]
    redis = get_redis()
    await redis.hset(f"doc:{doc_id}", "state", "paginating")
    if kind == "text":
        text = data.decode("utf-8", errors="replace")
        blocks = [
            Block(type="text", page_idx=0, text=part.strip()) for part in re.split(r"\n\s*\n", text) if part.strip()
        ]
        await redis.hset(f"doc:{doc_id}", "page_count", 1)
        await record_page(doc_id, 0, blocks)
        logger.info("paginate file=%s text paragraphs=%d", fields["filename"], len(blocks))
        return
    if kind == "html":
        blocks = await asyncio.to_thread(parse_html, data)
        await redis.hset(f"doc:{doc_id}", "page_count", 1)
        await record_page(doc_id, 0, blocks)
        logger.info("paginate file=%s html blocks=%d", fields["filename"], len(blocks))
        return
    if kind.startswith("image:"):
        image_bytes = await asyncio.to_thread(_cap_image_bytes, data)  # cap huge scans → keep the worker's RAM bounded
        await redis.hset(f"doc:{doc_id}", "page_count", 1)
        await redis.xadd(STREAM_PAGES, {"doc_id": doc_id, "page_idx": 0, "image": image_bytes})
        logger.info("paginate file=%s image", fields["filename"])
        return
    if kind in ("xlsx", "csv", "tsv", "json"):
        await redis.set(f"tabular:{doc_id}", data)  # no TTL: cleaned by cleanup() on completion or terminal failure
        await redis.hset(f"doc:{doc_id}", "mode", "tabular")
        if kind == "xlsx":
            names = await asyncio.to_thread(sheet_names, data)
            await redis.hset(f"doc:{doc_id}", "page_count", len(names))
            for sheet_no in range(1, len(names) + 1):
                await redis.xadd(STREAM_TABLES, {"doc_id": doc_id, "kind": kind, "sheet_no": sheet_no})
            logger.info("paginate file=%s sheets=%d", fields["filename"], len(names))
        else:
            await redis.hset(f"doc:{doc_id}", "page_count", 1)
            await redis.xadd(STREAM_TABLES, {"doc_id": doc_id, "kind": kind, "sheet_no": 0})
            logger.info("paginate file=%s kind=%s", fields["filename"], kind)
        return
    await redis.set(f"pdf:{doc_id}", data)  # no TTL: the source must outlive OCR; cleaned by cleanup()
    settings = get_settings()
    dpi = settings.digital_render_dpi if kind == "office-pdf" else settings.render_dpi
    loop = asyncio.get_running_loop()
    pages = await loop.run_in_executor(get_paginate_pool(), render_pdf_pages, data, dpi)
    await redis.hset(f"doc:{doc_id}", "page_count", len(pages))
    for idx, (image_bytes, digital) in enumerate(pages):
        await redis.xadd(
            STREAM_PAGES,
            {"doc_id": doc_id, "page_idx": idx, "image": image_bytes, "digital": "1" if digital else "0"},
        )
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
    )


async def record_page(doc_id: str, page_idx: int, blocks: list[Block]) -> None:
    redis = get_redis()
    # hsetnx: a reclaimed/duplicate delivery of the same page is a no-op (first write wins, no double-count)
    if not await redis.hsetnx(f"blocks:{doc_id}", str(page_idx), json.dumps([b.model_dump() for b in blocks])):
        return
    done = await redis.hincrby(f"doc:{doc_id}", "done_count", 1)
    expected = int(await redis.hget(f"doc:{doc_id}", "page_count") or 0)
    if expected and done == expected:  # exactly the page that completes the doc fires merge — once
        await redis.xadd(STREAM_MERGE, {"doc_id": doc_id})


async def fail_page(doc_id: str, page_idx: int) -> None:
    # one page exhausting retries must not fail the whole doc: record a marker, keep going
    await record_page(doc_id, page_idx, [Block(type="error", page_idx=page_idx, text="[extraction failed]")])


async def record_sheet(doc_id: str, sheet_no: int) -> None:
    redis = get_redis()
    if not await redis.hsetnx(f"sheets:{doc_id}", str(sheet_no), "1"):
        return
    done = await redis.hincrby(f"doc:{doc_id}", "done_count", 1)
    expected = int(await redis.hget(f"doc:{doc_id}", "page_count") or 0)
    if expected and done == expected:
        await redis.xadd(STREAM_MERGE, {"doc_id": doc_id})


async def handle_tabular(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    kind = fields["kind"]
    sheet_no = int(fields["sheet_no"])
    redis = get_redis()
    data = await redis.get(f"tabular:{doc_id}")
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
            tables = [(1, await asyncio.to_thread(read_csv_table, data, separator))]
        for _ordinal, table in tables:
            table.description = await describe_table(table.columns, table.sample_rows, filename)
        sheet_name = filename
    await save_sheet_tables(int(doc_id), sheet_no, sheet_name, tables)
    await record_sheet(doc_id, sheet_no)


async def fail_document(doc_id: str, stage: str) -> None:
    await get_redis().hset(f"doc:{doc_id}", mapping={"state": "failed", "error": f"{stage} failed"})
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
    client = get_mineru_client()
    if digital:
        # born-digital page: VLM does layout + recognizes only non-text; text/title come from the exact text layer
        content_blocks = await client.aio_two_step_extract(img, not_extract_list=list(LAYER_TYPES))
        text_blocks = [cb for cb in content_blocks if cb.type in LAYER_TYPES]
        if text_blocks:
            pdf_bytes = await get_redis().get(f"pdf:{doc_id}")
            loop = asyncio.get_running_loop()
            layer = await loop.run_in_executor(
                get_paginate_pool(),
                extract_layer_by_bbox,
                pdf_bytes,
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
    if not digital and get_settings().gap_fill:
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


async def handle_merge(fields: dict[str, str]) -> None:
    doc_id = fields["doc_id"]
    redis = get_redis()
    if (await redis.hget(f"doc:{doc_id}", "mode") or b"").decode() == "tabular":
        await finalize_tabular(int(doc_id))
        await redis.hset(f"doc:{doc_id}", "state", "done")
        logger.info("merge doc_id=%s state=done tabular", doc_id)
        return
    per_page = await redis.hgetall(f"blocks:{doc_id}")
    blocks: list[Block] = []
    for page_idx in sorted(int(k) for k in per_page):
        blocks.extend(Block(**raw) for raw in json.loads(per_page[str(page_idx).encode()]))
    source = (await redis.hget(f"doc:{doc_id}", "filename") or b"").decode()
    state = "partial" if any(b.type == "error" for b in blocks) else "done"
    await save_document_tree(int(doc_id), blocks, state)
    await persist_document_tree(int(doc_id))
    await redis.hset(f"doc:{doc_id}", "state", state)
    t0 = float(await redis.hget(f"doc:{doc_id}", "t0") or 0)
    logger.info("merge file=%s state=%s blocks=%d dur=%.1fs", source, state, len(blocks), time.time() - t0)


async def cleanup(doc_id: str) -> None:
    redis = get_redis()
    await redis.delete(f"blocks:{doc_id}", f"pdf:{doc_id}", f"tabular:{doc_id}", f"sheets:{doc_id}")
    await redis.expire(f"doc:{doc_id}", 3600)  # keep final status briefly, then auto-evict — no accumulation


# ---- stage wiring (used by the worker entrypoint) -------------------------------------

STREAMS = (STREAM_INGEST, STREAM_NORMALIZED, STREAM_PAGES, STREAM_MERGE, STREAM_TABLES)


async def ensure_group(stream: str) -> None:
    try:
        await get_redis().xgroup_create(stream, GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise

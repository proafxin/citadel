import asyncio
import io
import json
import logging
import re
import shutil
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import cv2
import filetype
import numpy as np
import pypdfium2 as pdfium
import redis.asyncio as aioredis
from markdownify import markdownify
from mineru_vl_utils import MinerUClient
from mineru_vl_utils.structs import ContentBlock, ExtractResult
from PIL import Image
from rapidocr_onnxruntime import RapidOCR
from redis.exceptions import ResponseError

from citadel.schemas.content import Block
from citadel.services import persistence
from config import get_settings

logger = logging.getLogger(__name__)

GROUP = "citadel"
STREAM_INGEST = "ingest"
STREAM_NORMALIZED = "normalized"
STREAM_PAGES = "pages"
STREAM_MERGE = "merge"
STREAM_TABLES = "tables"
STREAM_GAPFILL = "gapfill"  # decoupled CPU stage: scanned pages do RapidOCR gap-fill here, off the GPU OCR slot

OFFICE_EXTS = {"doc", "docx", "ppt", "pptx", "odt", "odp", "rtf"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"}
TEXT_EXTS = {"txt", "md", "markdown"}
HTML_EXTS = {"html", "htm"}

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
    # n dedicated LibreOffice profile dirs; a worker holds one per job so concurrent soffice don't collide
    base = get_settings().data_dir / "lo_profiles"
    base.mkdir(parents=True, exist_ok=True)
    queue: asyncio.Queue[str] = asyncio.Queue()
    for i in range(n):
        profile = base / str(i)
        profile.mkdir(exist_ok=True)
        queue.put_nowait(str(profile.resolve()))
    return queue


def _raw_dir() -> Path:
    return get_settings().data_dir / "raw"


def _norm_dir() -> Path:
    return get_settings().data_dir / "normalized"


# ---- orchestrator side ----------------------------------------------------------------


async def submit_document(data: bytes, filename: str, library: str) -> str:
    doc_id = str(await persistence.create_document(library, filename))
    redis = get_redis()
    _raw_dir().mkdir(parents=True, exist_ok=True)
    (_raw_dir() / doc_id).write_bytes(data)
    await redis.hset(
        f"doc:{doc_id}",
        mapping={"state": "queued", "filename": filename, "done_count": 0, "t0": time.time()},
    )
    await redis.xadd(STREAM_INGEST, {"doc_id": doc_id, "filename": filename})
    logger.info("ingest file=%s doc_id=%s", filename, doc_id)
    return doc_id


async def get_status(doc_id: str) -> dict[str, str]:
    raw = await get_redis().hgetall(f"doc:{doc_id}")
    return {k.decode(): v.decode() for k, v in raw.items()}


async def get_result(doc_id: str) -> dict | None:
    return await persistence.get_document_blocks(int(doc_id))


# ---- normalize stage (CPU / process pool) ---------------------------------------------


def _detect_ext(path: Path, filename: str) -> str:
    guess = filetype.guess(str(path))
    if guess is not None:
        return guess.extension
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def normalize_file(raw_path_str: str, doc_id: str, filename: str, profile_dir: str) -> str:
    raw_path = Path(raw_path_str)
    ext = _detect_ext(raw_path, filename)
    _norm_dir().mkdir(parents=True, exist_ok=True)
    if ext in OFFICE_EXTS:
        soffice = shutil.which("soffice")
        if soffice is None:
            raise RuntimeError("libreoffice 'soffice' not found on PATH")
        subprocess.run(
            [
                soffice,
                f"-env:UserInstallation=file://{profile_dir}",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(_norm_dir()),
                str(raw_path),
            ],
            check=True,
            capture_output=True,
        )
        produced = _norm_dir() / f"{raw_path.stem}.pdf"
        target = _norm_dir() / f"{doc_id}.pdf"
        produced.rename(target)
        return "office-pdf"  # born-digital (libreoffice produced it) → paginate renders at the lower dpi
    if ext == "pdf":
        shutil.copyfile(raw_path, _norm_dir() / f"{doc_id}.pdf")
        return "pdf"
    if ext in IMAGE_EXTS:
        shutil.copyfile(raw_path, _norm_dir() / f"{doc_id}.{ext}")
        return f"image:{ext}"
    if ext in HTML_EXTS:
        (_norm_dir() / f"{doc_id}.md").write_text(markdownify(raw_path.read_text(encoding="utf-8")))
        return "text"
    # txt / md / unknown → treat as text
    (_norm_dir() / f"{doc_id}.md").write_text(raw_path.read_text(encoding="utf-8", errors="replace"))
    return "text"


async def handle_normalize(fields: dict[str, str], profile_dir: str) -> None:
    doc_id = fields["doc_id"]
    await get_redis().hset(f"doc:{doc_id}", "state", "normalizing")
    kind = await asyncio.to_thread(normalize_file, str(_raw_dir() / doc_id), doc_id, fields["filename"], profile_dir)
    await get_redis().xadd(STREAM_NORMALIZED, {"doc_id": doc_id, "kind": kind, "filename": fields["filename"]})
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


def render_pdf_pages(pdf_path_str: str, dpi: int) -> list[tuple[bytes, bool]]:
    # (png, is_digital) per page; is_digital = has a real text layer and isn't rotated → safe to read by bbox
    pages: list[tuple[bytes, bool]] = []
    pdf = pdfium.PdfDocument(pdf_path_str)
    scale = dpi / 72
    for page in pdf:
        bio = io.BytesIO()
        _downscale(page.render(scale=scale).to_pil()).save(bio, format="PNG")
        digital = page.get_rotation() == 0 and page.get_textpage().count_chars() > 16
        pages.append((bio.getvalue(), digital))
    pdf.close()
    return pages


def extract_text_by_bbox(pdf_path_str: str, page_idx: int, bboxes: list[list[float]]) -> list[str]:
    # exact text from the PDF text layer inside each normalized (0-1, top-left) bbox; pdfium origin is bottom-left
    pdf = pdfium.PdfDocument(pdf_path_str)
    page = pdf[page_idx]
    width, height = page.get_size()
    textpage = page.get_textpage()
    texts = [
        textpage.get_text_bounded(
            left=x0 * width, bottom=(1 - y1) * height, right=x1 * width, top=(1 - y0) * height
        ).strip()
        for x0, y0, x1, y1 in bboxes
    ]
    pdf.close()
    return texts


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
        raw = (_norm_dir() / f"{doc_id}.{kind.split(':', 1)[1]}").read_bytes()
        image_bytes = await asyncio.to_thread(_cap_image_bytes, raw)  # cap huge scans → keep the worker's RAM bounded
        await redis.hset(f"doc:{doc_id}", "page_count", 1)
        await redis.xadd(STREAM_PAGES, {"doc_id": doc_id, "page_idx": 0, "image": image_bytes})
        logger.info("paginate file=%s image", fields["filename"])
        return
    settings = get_settings()
    dpi = settings.digital_render_dpi if kind == "office-pdf" else settings.render_dpi
    loop = asyncio.get_running_loop()
    pages = await loop.run_in_executor(get_paginate_pool(), render_pdf_pages, str(_norm_dir() / f"{doc_id}.pdf"), dpi)
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
        list_items=list(get("list_items") or []),
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


async def fail_document(doc_id: str, stage: str) -> None:
    await get_redis().hset(f"doc:{doc_id}", mapping={"state": "failed", "error": f"{stage} failed"})


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
            loop = asyncio.get_running_loop()
            texts = await loop.run_in_executor(
                get_paginate_pool(),
                extract_text_by_bbox,
                str(_norm_dir() / f"{doc_id}.pdf"),
                page_idx,
                [list(cb.bbox) for cb in text_blocks],
            )
            for cb, text in zip(text_blocks, texts, strict=False):
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
    # finalize a page: forward any tables and record it (records exactly once → drives the merge barrier)
    for block in blocks:
        if block.type == "table":
            await get_redis().xadd(STREAM_TABLES, {"doc_id": doc_id, "page_idx": page_idx, "html": block.text})
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
    per_page = await redis.hgetall(f"blocks:{doc_id}")
    blocks: list[Block] = []
    for page_idx in sorted(int(k) for k in per_page):
        blocks.extend(Block(**raw) for raw in json.loads(per_page[str(page_idx).encode()]))
    source = (await redis.hget(f"doc:{doc_id}", "filename") or b"").decode()
    state = "partial" if any(b.type == "error" for b in blocks) else "done"
    await persistence.save_document_result(int(doc_id), blocks, state)
    await redis.hset(f"doc:{doc_id}", "state", state)
    t0 = float(await redis.hget(f"doc:{doc_id}", "t0") or 0)
    logger.info("merge file=%s state=%s blocks=%d dur=%.1fs", source, state, len(blocks), time.time() - t0)


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

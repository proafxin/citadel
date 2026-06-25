import asyncio
import hashlib
import io
import json
import logging
import re
import shutil
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

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
LAYER_TYPES = ("text", "title")  # filled from the PDF text layer on born-digital pages (skip VLM recognition)


@lru_cache
def get_redis() -> aioredis.Redis:
    # socket_timeout=None so the blocking XREADGROUP isn't cut off by a client read timeout
    return aioredis.from_url(get_settings().redis_url, socket_timeout=None)


@lru_cache
def get_mineru_client() -> MinerUClient:
    return MinerUClient(backend="http-client", server_url=get_settings().mineru_base_url, use_tqdm=False)


@lru_cache
def get_rapidocr() -> RapidOCR:
    return RapidOCR()  # bundled PP-OCR det/rec on CPU — recovers scanned text the VLM dropped, no GPU/VRAM


@lru_cache
def get_rapidocr_pool() -> ThreadPoolExecutor:
    # onnxruntime releases the GIL during inference, so threads give real parallelism; bound the CPU concurrency
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


def _result_dir() -> Path:
    return get_settings().data_dir / "results"


# ---- orchestrator side ----------------------------------------------------------------


async def submit_document(data: bytes, filename: str) -> str:
    doc_id = hashlib.sha256(data).hexdigest()  # deterministic per content → same file reuses its key, no flood
    redis = get_redis()
    await redis.delete(f"doc:{doc_id}", f"blocks:{doc_id}")  # clean slate so a re-ingest reprocesses from scratch
    (_result_dir() / f"{doc_id}.json").unlink(missing_ok=True)
    _raw_dir().mkdir(parents=True, exist_ok=True)
    (_raw_dir() / doc_id).write_bytes(data)
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


def render_pdf_pages(pdf_path_str: str, dpi: int) -> list[tuple[bytes, bool]]:
    # (png, is_digital) per page; is_digital = has a real text layer and isn't rotated → safe to read by bbox
    pages: list[tuple[bytes, bool]] = []
    pdf = pdfium.PdfDocument(pdf_path_str)
    scale = dpi / 72
    for page in pdf:
        bio = io.BytesIO()
        page.render(scale=scale).to_pil().save(bio, format="PNG")
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
        image_bytes = (_norm_dir() / f"{doc_id}.{kind.split(':', 1)[1]}").read_bytes()
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
    if not await redis.hsetnx(f"blocks:{doc_id}", str(page_idx), json.dumps([asdict(b) for b in blocks])):
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
        if text and str(text).strip():
            block.content = str(text)


async def ocr_empty_blocks(client: MinerUClient, img: Image.Image, content_blocks: ExtractResult) -> None:
    # image blocks the VLM localized but left empty (seals, stamps, figures, logos) → OCR the crop as text.
    # the VLM never read these (it only localizes images), so this is a fresh request, not a failed retry
    await _vlm_recover(client, img, [cb for cb in content_blocks if cb.type == "image" and not (cb.content or "").strip()])


async def recover_digital_handwriting(client: MinerUClient, img: Image.Image, text_blocks: list[ContentBlock]) -> None:
    # digital page: text blocks the layer left empty or that are fill-in fields may carry handwriting the layer
    # can't see → VLM-OCR the crop (a fresh attempt — the VLM never read these blocks on a digital page)
    flagged = [cb for cb in text_blocks if not (cb.content or "").strip() or FILLIN.search(cb.content or "")]
    await _vlm_recover(client, img, flagged)


def rapidocr_recover(page: np.ndarray, blocks: ExtractResult) -> list[tuple[list[float], str]]:
    # det every text box; keep only the ones no VLM block covers (the dropped regions); rec just those → (bbox, text)
    engine = get_rapidocr()
    height, width = page.shape[:2]
    boxes, _ = engine(page, use_det=True, use_cls=False, use_rec=False)
    out: list[tuple[list[float], str]] = []
    for box in boxes or []:
        x0, y0 = int(min(p[0] for p in box)), int(min(p[1] for p in box))
        x1, y1 = int(max(p[0] for p in box)), int(max(p[1] for p in box))
        if x1 <= x0 or y1 <= y0:
            continue
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if any(
            b.bbox[0] * width <= cx <= b.bbox[2] * width and b.bbox[1] * height <= cy <= b.bbox[3] * height
            for b in blocks
        ):
            continue
        result, _ = engine(page[y0:y1, x0:x1], use_det=False, use_cls=False, use_rec=True)
        text = result[0][0] if result else ""
        if text.strip():
            out.append(([x0 / width, y0 / height, x1 / width, y1 / height], text))
    return out


async def recover_scanned_gaps(img: Image.Image, content_blocks: ExtractResult) -> None:
    # scanned page: the VLM read most text but can drop a region; RapidOCR (CPU) detects every text box and
    # recognizes only the ones no VLM block covers, then adds them as blocks — matches stock's det→rec-gaps pass
    loop = asyncio.get_running_loop()
    page = np.asarray(img.convert("RGB"))
    recovered = await loop.run_in_executor(get_rapidocr_pool(), rapidocr_recover, page, content_blocks)
    for bbox, text in recovered:
        content_blocks.append(ContentBlock("text", bbox, content=text))


async def handle_ocr(fields: dict[str, str], image: bytes) -> None:
    doc_id = fields["doc_id"]
    page_idx = int(fields["page_idx"])
    img = Image.open(io.BytesIO(image))
    client = get_mineru_client()
    if fields.get("digital") == "1":
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
            for cb, text in zip(text_blocks, texts):
                if text:
                    cb.content = text
            await recover_digital_handwriting(client, img, text_blocks)
    else:
        content_blocks = await client.aio_two_step_extract(img)
        await recover_scanned_gaps(img, content_blocks)
    await ocr_empty_blocks(client, img, content_blocks)
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
        blocks.extend(Block(**raw) for raw in json.loads(per_page[str(page_idx).encode()]))
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

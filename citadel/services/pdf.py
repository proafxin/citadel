import io
import re

import pypdfium2 as pdfium
from cachetools import LRUCache
from PIL import Image
from pydantic import BaseModel

MAX_IMAGE_SIDE = 2500
PDF_CACHE_MAX = 4


class _PdfCache(LRUCache[str, pdfium.PdfDocument]):
    def popitem(self) -> tuple[str, pdfium.PdfDocument]:
        key, pdf = super().popitem()
        pdf.close()
        return key, pdf


_pdf_cache: _PdfCache = _PdfCache(maxsize=PDF_CACHE_MAX)


def _open_pdf(path: str) -> pdfium.PdfDocument:
    pdf = _pdf_cache.get(path)
    if pdf is None:
        pdf = pdfium.PdfDocument(path)
        _pdf_cache[path] = pdf
    return pdf


def downscale(img: Image.Image) -> Image.Image:
    if max(img.size) <= MAX_IMAGE_SIDE:
        return img
    scale = MAX_IMAGE_SIDE / max(img.size)
    return img.resize((round(img.width * scale), round(img.height * scale)))


def count_pdf_pages(path: str) -> int:
    with pdfium.PdfDocument(path) as pdf:
        return len(pdf)


def render_pdf_page(path: str, page_idx: int, dpi: int) -> tuple[bytes, bool]:
    pdf = _open_pdf(path)
    page = pdf[page_idx]
    try:
        scale = min(dpi / 72, MAX_IMAGE_SIDE / max(page.get_size()))
        bitmap = page.render(scale=scale)
        try:
            bio = io.BytesIO()
            bitmap.to_pil().save(bio, format="PNG")
        finally:
            bitmap.close()
        textpage = page.get_textpage()
        try:
            digital = page.get_rotation() == 0 and textpage.count_chars() > 16
        finally:
            textpage.close()
        return bio.getvalue(), digital
    finally:
        page.close()


MIN_RESCUE_CHARS = 8
RESCUE_GAP = 1.8
_WORD = re.compile(r"[^\W\d_]{2,}")


class LayerRun(BaseModel):
    text: str
    bbox: list[float]


def _is_content(text: str) -> bool:
    return _WORD.search(text) is not None


def uncovered_layer_runs(path: str, page_idx: int, bboxes: list[list[float]]) -> list[LayerRun]:
    pdf = _open_pdf(path)
    page = pdf[page_idx]
    try:
        width, height = page.get_size()
        textpage = page.get_textpage()
        try:
            rects = [(x0 * width, (1 - y1) * height, x1 * width, (1 - y0) * height) for x0, y0, x1, y1 in bboxes]
            return _walk_uncovered(textpage, rects, width, height)
        finally:
            textpage.close()
    finally:
        page.close()


def _covered(box: tuple[float, float, float, float], rects: list[tuple[float, float, float, float]]) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return any(left <= cx <= right and bottom <= cy <= top for left, bottom, right, top in rects)


def _flush(chars: list[tuple[str, tuple[float, float, float, float]]], width: float, height: float) -> LayerRun | None:
    text = "".join(char for char, _ in chars).strip()
    if len(text) < MIN_RESCUE_CHARS or not _is_content(text):
        return None
    left = min(box[0] for _, box in chars)
    bottom = min(box[1] for _, box in chars)
    right = max(box[2] for _, box in chars)
    top = max(box[3] for _, box in chars)
    return LayerRun(
        text=text,
        bbox=[left / width, 1 - top / height, right / width, 1 - bottom / height],
    )


def _breaks(previous: tuple[float, float, float, float], box: tuple[float, float, float, float]) -> bool:
    line_height = max(previous[3] - previous[1], 1.0)
    return abs(box[1] - previous[1]) > line_height * RESCUE_GAP or box[0] < previous[0] - line_height * RESCUE_GAP


def _walk_uncovered(
    textpage: pdfium.PdfTextPage, rects: list[tuple[float, float, float, float]], width: float, height: float
) -> list[LayerRun]:
    runs: list[LayerRun] = []
    current: list[tuple[str, tuple[float, float, float, float]]] = []
    for index in range(textpage.count_chars()):
        char = textpage.get_text_range(index, 1)
        box = textpage.get_charbox(index, loose=True)
        if not char.strip():
            if current:
                current.append((char, current[-1][1]))
            continue
        if box is None or _covered(box, rects):
            continue
        if current and _breaks(current[-1][1], box):
            run = _flush(current, width, height)
            if run:
                runs.append(run)
            current = []
        current.append((char, box))
    run = _flush(current, width, height)
    if run:
        runs.append(run)
    return runs


def extract_layer_by_bbox(path: str, page_idx: int, bboxes: list[list[float]]) -> list[str]:
    pdf = _open_pdf(path)
    page = pdf[page_idx]
    try:
        width, height = page.get_size()
        textpage = page.get_textpage()
        try:
            out: list[str] = []
            for x0, y0, x1, y1 in bboxes:
                left, right, bottom, top = x0 * width, x1 * width, (1 - y1) * height, (1 - y0) * height
                out.append(textpage.get_text_bounded(left=left, bottom=bottom, right=right, top=top).strip())
            return out
        finally:
            textpage.close()
    finally:
        page.close()

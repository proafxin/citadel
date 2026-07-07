import io
from collections import OrderedDict

import pypdfium2 as pdfium
from PIL import Image

MAX_IMAGE_SIDE = 2500
PDF_CACHE_MAX = 4  # open pdfium handles kept per (long-lived) pool worker; mmap-cheap, so repeat pages skip the reparse

_pdf_cache: OrderedDict[str, pdfium.PdfDocument] = OrderedDict()


def _open_pdf(path: str) -> pdfium.PdfDocument:
    # one process runs one pool task at a time, so this per-process cache needs no lock. bounded + close-on-evict so a
    # long-lived worker never accumulates handles (also caps the leak a hung/abandoned doc could otherwise cause)
    cached = _pdf_cache.get(path)
    if cached is not None:
        _pdf_cache.move_to_end(path)
        return cached
    pdf = pdfium.PdfDocument(path)
    _pdf_cache[path] = pdf
    while len(_pdf_cache) > PDF_CACHE_MAX:
        _, evicted = _pdf_cache.popitem(last=False)
        evicted.close()
    return pdf


def downscale(img: Image.Image) -> Image.Image:
    if max(img.size) <= MAX_IMAGE_SIDE:
        return img
    scale = MAX_IMAGE_SIDE / max(img.size)
    return img.resize((round(img.width * scale), round(img.height * scale)))


def count_pdf_pages(path: str) -> int:
    return len(_open_pdf(path))


def render_pdf_page(path: str, page_idx: int, dpi: int) -> tuple[bytes, bool]:
    # the pdf handle is cache-owned (never closed here); page/bitmap/textpage are per-call and released even on error,
    # so a raise mid-render can't strand a large bitmap buffer in the long-lived pool worker
    pdf = _open_pdf(path)
    page = pdf[page_idx]
    try:
        scale = min(dpi / 72, MAX_IMAGE_SIDE / max(page.get_size()))  # cap BEFORE render → never alloc oversized bitmap
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

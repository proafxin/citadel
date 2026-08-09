import io

import pypdfium2 as pdfium
from cachetools import LRUCache
from PIL import Image

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


def render_pdf_page(path: str, page_idx: int, dpi: int) -> bytes:
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
        return bio.getvalue()
    finally:
        page.close()

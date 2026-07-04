import io

import pypdfium2 as pdfium
from PIL import Image

MAX_IMAGE_SIDE = 2500


def downscale(img: Image.Image) -> Image.Image:
    if max(img.size) <= MAX_IMAGE_SIDE:
        return img
    scale = MAX_IMAGE_SIDE / max(img.size)
    return img.resize((round(img.width * scale), round(img.height * scale)))


def render_pdf_pages(pdf_bytes: bytes, dpi: int) -> list[tuple[bytes, bool]]:
    pages: list[tuple[bytes, bool]] = []
    pdf = pdfium.PdfDocument(pdf_bytes)
    scale = dpi / 72
    for page in pdf:
        bio = io.BytesIO()
        downscale(page.render(scale=scale).to_pil()).save(bio, format="PNG")
        digital = page.get_rotation() == 0 and page.get_textpage().count_chars() > 16
        pages.append((bio.getvalue(), digital))
    pdf.close()
    return pages


def extract_layer_by_bbox(pdf_bytes: bytes, page_idx: int, bboxes: list[list[float]]) -> list[str]:
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

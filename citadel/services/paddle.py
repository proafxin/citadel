import html
import io
import itertools
import math
import re
from base64 import b64encode
from functools import lru_cache

import httpx
from PIL import Image

from citadel.services.detect import DetBlock
from config import PADDLEOCR_MODEL, get_settings

CROP_CONCURRENCY = 128  # the ONE bottleneck: a crop is the unit of VLM work, and this semaphore caps what is
# outstanding at the model at all (Running + Waiting). it lives HERE, at the model boundary, because it must equal the
# server's --max-num-seqs: below that the GPU cannot fill its sequence slots, above it the excess merely queues INSIDE
# the model, where every waiting request pins a decoded image
VLM_CLIENTS = 16
VLM_CONN_HEADROOM = 4  # sockets per client as a MULTIPLE of that client's mean share of the crop semaphore. derived,
# never a literal: a socket count is not a concurrency bound and must never become one. httpx blocks a request that
# cannot get a connection INSIDE the post, so it would sit there holding its crop permit and sending nothing, and the
# model would starve with nothing in our logs to say so
VLM_CONN_PER_CLIENT = VLM_CONN_HEADROOM * CROP_CONCURRENCY // VLM_CLIENTS
VLM_TIMEOUT = 600
VLM_MAX_TOKENS = 8192  # a dense table is the longest thing the model emits; below this it would truncate mid-row

# the prompts are the model's own task selectors — four tasks, chosen by what the detector says the region IS.
# taken verbatim from the reference pipeline; they are not ours to reword
PROMPT_OCR = "OCR:"
PROMPT_TABLE = "Table Recognition:"
PROMPT_FORMULA = "Formula Recognition:"
PROMPT_CHART = "Chart Recognition:"
PROMPT_SEAL = "Seal Recognition:"

# regions the model is never asked to read: a photo or a figure has no text to recognize. seals and charts are NOT here
# — a seal carries the stamp's words and a chart carries its data, and both are asked for explicitly
PICTURE_LABELS = frozenset({"image", "header_image", "footer_image"})

# a crop is smart-resized into this pixel window before it is sent, exactly as the reference pipeline does. the floor
# stops a one-line crop from arriving too small to read; the ceiling bounds the image tokens a huge table can cost
MIN_PIXELS = 112_896
MAX_PIXELS = 1_003_520

# born-digital pages take their characters from the PDF's own text layer instead of asking the model to re-recognize
# text it can already read exactly. these are the prose regions that have real characters behind them
LAYER_LABELS = frozenset(
    {
        "text",
        "doc_title",
        "paragraph_title",
        "abstract",
        "content",
        "footnote",
        "reference",
        "reference_content",
        "vertical_text",
        "vision_footnote",
        "formula_number",
    }
)


def prompt_for(label: str) -> str:
    if label == "table":
        return PROMPT_TABLE
    if label == "chart":
        return PROMPT_CHART
    if label == "seal":
        return PROMPT_SEAL
    if "formula" in label and label != "formula_number":  # formula_number is "(21)" — prose, not mathematics
        return PROMPT_FORMULA
    return PROMPT_OCR


def is_readable(block: DetBlock) -> bool:
    # inline_formula is dropped, not skipped-with-a-hole: it lies INSIDE a text region, and the OCR prompt on that
    # region already returns the mathematics inline as LaTeX. cropping it separately would emit the same content twice
    return block.label not in PICTURE_LABELS and block.label != "inline_formula"


def resize_for_vlm(crop: Image.Image) -> Image.Image:
    pixels = crop.width * crop.height
    if pixels == 0 or MIN_PIXELS <= pixels <= MAX_PIXELS:
        return crop
    scale = math.sqrt((MIN_PIXELS if pixels < MIN_PIXELS else MAX_PIXELS) / pixels)
    return crop.resize(
        (max(1, round(crop.width * scale)), max(1, round(crop.height * scale))), Image.Resampling.LANCZOS
    )


# ---- OTSL → HTML -------------------------------------------------------------------------------------
# the model emits tables in OTSL, a token-efficient grid language: one token per cell rather than a styled <td>. every
# consumer downstream (grid extraction, header detection, the canonical Table) is written against HTML, so translate
# here and nothing else has to change. <fcel> full cell, <ecel> empty cell, <lcel>/<ucel>/<xcel> continue a span from
# the left / above / both, <nl> ends a row.
_OTSL_TOKEN = re.compile(r"<(fcel|ecel|lcel|ucel|xcel|nl|ched|rhed|srow)>")


def otsl_to_grid(otsl: str) -> list[list[str]]:
    grid: list[list[str]] = []
    row: list[str] = []
    parts = _OTSL_TOKEN.split(otsl)
    for token, content in itertools.zip_longest(parts[1::2], parts[2::2], fillvalue=""):
        text = str(content).strip()
        match token:
            case "nl":
                grid.append(row)
                row = []
            case "fcel":
                row.append(text)
            case "ecel":
                row.append("")
            case "lcel":  # spans expanded, never left blank: a flattened span repeats its value in every cell it covers
                row.append(row[-1] if row else "")
            case "ucel" | "xcel":
                above = grid[-1] if grid else []
                row.append(above[len(row)] if len(row) < len(above) else "")
            case _:  # ched/rhed/srow mark header rows; the header detector decides that itself, from the grid
                if text:
                    row.append(text)
    if row:
        grid.append(row)
    return [r for r in grid if any(cell for cell in r)]


def otsl_to_html(otsl: str) -> str:
    grid = otsl_to_grid(otsl)
    if not grid:
        return ""
    width = max(len(r) for r in grid)
    rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(r[i]) if i < len(r) else ''}</td>" for i in range(width)) + "</tr>"
        for r in grid
    )
    return f"<table>{rows}</table>"


# ---- the recognition model, served by vLLM ------------------------------------------------------------


@lru_cache
def _vlm_pool() -> list[httpx.AsyncClient]:
    # one client per slot, each with its OWN connection pool: a single shared pool makes httpcore walk every connection
    # on every event, and at this concurrency that pegs the event loop
    settings = get_settings()
    return [
        httpx.AsyncClient(
            base_url=settings.paddleocr_base_url,
            timeout=VLM_TIMEOUT,
            limits=httpx.Limits(max_connections=VLM_CONN_PER_CLIENT, max_keepalive_connections=VLM_CONN_PER_CLIENT),
        )
        for _ in range(VLM_CLIENTS)
    ]


_VLM_RR = itertools.count()


def get_vlm_client() -> httpx.AsyncClient:
    return _vlm_pool()[next(_VLM_RR) % VLM_CLIENTS]


async def close_vlm_clients() -> None:
    for client in _vlm_pool():
        await client.aclose()
    _vlm_pool.cache_clear()


def png_bytes(crop: Image.Image) -> bytes:
    buffer = io.BytesIO()
    crop.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


async def recognize(payload: bytes, prompt: str) -> str:
    # one crop, one request. the payload is already PNG bytes: the raw pixels died at cut time, so what waits out the
    # queue here is the compressed form, not a bitmap
    body = {
        "model": PADDLEOCR_MODEL,
        "max_tokens": VLM_MAX_TOKENS,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64encode(payload).decode()}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    response = await get_vlm_client().post("/v1/chat/completions", json=body)
    response.raise_for_status()
    content: str = response.json()["choices"][0]["message"]["content"]
    return content.strip()


def block_text(label: str, raw: str) -> str:
    # tables arrive as OTSL and every consumer downstream reads HTML; everything else is already its final form —
    # prose as prose, mathematics as LaTeX
    return otsl_to_html(raw) if label == "table" else raw

import html
import io
import itertools
import logging
import math
import re
from base64 import b64encode
from dataclasses import dataclass, field
from functools import lru_cache

import httpx
from PIL import Image

from citadel.services.detect import DetBlock
from config import PADDLEOCR_MODEL, get_settings

logger = logging.getLogger(__name__)

CROP_CONCURRENCY = 128

VLM_KEEPALIVE_EXPIRY = 2.0
VLM_CLIENTS = 16
VLM_CONN_HEADROOM = 4
VLM_CONN_PER_CLIENT = VLM_CONN_HEADROOM * CROP_CONCURRENCY // VLM_CLIENTS
VLM_TIMEOUT = 600
VLM_MAX_TOKENS = 4096

PROMPT_OCR = "OCR:"
PROMPT_TABLE = "Table Recognition:"
PROMPT_FORMULA = "Formula Recognition:"
PROMPT_CHART = "Chart Recognition:"
PROMPT_SEAL = "Seal Recognition:"

PICTURE_LABELS = frozenset({"image", "header_image", "footer_image"})

MIN_PIXELS = 112_896
MAX_PIXELS = 1_003_520

LAYER_LABELS = frozenset(
    {
        "header",
        "footer",
        "number",
        "aside_text",
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
    if "formula" in label and label != "formula_number":
        return PROMPT_FORMULA
    return PROMPT_OCR


def is_readable(block: DetBlock) -> bool:
    return block.label not in PICTURE_LABELS and block.label != "inline_formula"


PATCH_PIXELS = 14 * 14 * 2 * 2
FLOOR_TOKENS = MIN_PIXELS // PATCH_PIXELS
_SIZE_BUCKETS = (0.125, 0.25, 0.5, 1.0)


@dataclass
class _CropSizes:
    count: int = 0
    floor_bound: int = 0
    source_pixels: int = 0
    sent_pixels: int = 0
    buckets: list[int] = field(default_factory=lambda: [0] * (len(_SIZE_BUCKETS) + 1))


@lru_cache
def get_crop_sizes() -> _CropSizes:
    return _CropSizes()


def _record_crop_size(source: int, sent: int) -> None:
    sizes = get_crop_sizes()
    sizes.count += 1
    sizes.source_pixels += source
    sizes.sent_pixels += sent
    if source < MIN_PIXELS:
        sizes.floor_bound += 1
    ratio = source / MIN_PIXELS
    index = next((i for i, edge in enumerate(_SIZE_BUCKETS) if ratio < edge), len(_SIZE_BUCKETS))
    sizes.buckets[index] += 1


def log_crop_sizes() -> None:
    sizes = get_crop_sizes()
    if not sizes.count:
        return
    edges = ["<1/8", "1/8-1/4", "1/4-1/2", "1/2-1", ">=1"]
    spread = " ".join(f"{name}={value}" for name, value in zip(edges, sizes.buckets, strict=True))
    billed = sizes.sent_pixels / PATCH_PIXELS
    content = sizes.source_pixels / PATCH_PIXELS
    logger.info(
        "crops n=%d at_floor=%.1f%% billed=%.0fk tok (%.0f/crop, floor=%d) content=%.0fk tok "
        "packing_headroom=%.1f%% | %s",
        sizes.count,
        100 * sizes.floor_bound / sizes.count,
        billed / 1e3,
        billed / sizes.count,
        FLOOR_TOKENS,
        content / 1e3,
        100 * (billed - content) / billed if billed else 0.0,
        spread,
    )
    get_crop_sizes.cache_clear()


def resize_for_vlm(crop: Image.Image) -> Image.Image:
    pixels = crop.width * crop.height
    if pixels == 0 or MIN_PIXELS <= pixels <= MAX_PIXELS:
        _record_crop_size(pixels, pixels)
        return crop
    scale = math.sqrt((MIN_PIXELS if pixels < MIN_PIXELS else MAX_PIXELS) / pixels)
    resized = crop.resize(
        (max(1, round(crop.width * scale)), max(1, round(crop.height * scale))), Image.Resampling.LANCZOS
    )
    _record_crop_size(pixels, resized.width * resized.height)
    return resized


_OTSL_TOKEN = re.compile(r"<(fcel|ecel|lcel|ucel|xcel|nl|ched|rhed|srow)>")


def otsl_rows(otsl: str) -> list[tuple[list[str], bool, bool]]:
    rows: list[tuple[list[str], bool, bool]] = []
    row: list[str] = []
    header = False
    single_span = False
    parts = _OTSL_TOKEN.split(otsl)
    for marker, content in itertools.zip_longest(parts[1::2], parts[2::2], fillvalue=""):
        text = str(content).strip()
        match marker:
            case "nl":
                rows.append((row, header, single_span and len(row) > 1))
                row = []
                header = False
                single_span = False
            case "fcel":
                row.append(text)
                single_span = len(row) == 1
            case "ecel":
                row.append("")
                single_span = False
            case "lcel":
                row.append(row[-1] if row else "")
            case "ucel" | "xcel":
                above = rows[-1][0] if rows else []
                row.append(above[len(row)] if len(row) < len(above) else "")
                single_span = False
            case _:
                header = header or marker == "ched"
                if text:
                    row.append(text)
                single_span = False
    if row:
        rows.append((row, header, single_span and len(row) > 1))
    return [(cells, flag, span) for cells, flag, span in rows if any(cell for cell in cells)]


def otsl_to_json(otsl: str) -> list[list[dict]]:
    output_rows: list[list[dict]] = []
    track_row: list[dict] = []
    output_row: list[dict] = []
    track_above: list[dict] = []
    parts = _OTSL_TOKEN.split(otsl)
    for marker, content in itertools.zip_longest(parts[1::2], parts[2::2], fillvalue=""):
        text = str(content).strip()
        match marker:
            case "nl":
                output_rows.append(output_row)
                track_above = track_row
                track_row = []
                output_row = []
            case "fcel" | "ecel":
                cell = {"text": text if marker == "fcel" else "", "rowspan": 1, "colspan": 1, "is_header": False}
                track_row.append(cell)
                output_row.append(cell)
            case "ched" | "rhed":
                cell = {"text": text, "rowspan": 1, "colspan": 1, "is_header": True}
                track_row.append(cell)
                output_row.append(cell)
            case "lcel":
                if track_row:
                    track_row[-1]["colspan"] += 1
                    track_row.append(track_row[-1])
            case "ucel" | "xcel":
                col = len(track_row)
                if col < len(track_above):
                    origin = track_above[col]
                    origin["rowspan"] += 1
                    track_row.append(origin)
    if output_row or track_row:
        output_rows.append(output_row)
    return [row for row in output_rows if row]


def _row_html(cells: list[str], width: int, tag: str) -> str:
    body = "".join(f"<{tag}>{html.escape(cells[i]) if i < len(cells) else ''}</{tag}>" for i in range(width))
    return f"<tr>{body}</tr>"


def _blank_full_width_spans(rows: list[tuple[list[str], bool, bool]], width: int) -> list[tuple[list[str], bool]]:
    out: list[tuple[list[str], bool]] = []
    for cells, header, span in rows:
        collapsed = [cells[0], *([""] * (len(cells) - 1))] if span and len(cells) == width else cells
        out.append((collapsed, header))
    return out


def otsl_to_html(otsl: str) -> str:
    rows = otsl_rows(otsl)
    if not rows:
        return ""
    width = max(len(cells) for cells, _, _ in rows)
    collapsed = _blank_full_width_spans(rows, width)
    body = "".join(_row_html(cells, width, "th" if flag else "td") for cells, flag in collapsed)
    return f"<table>{body}</table>"


@lru_cache
def _vlm_pool() -> list[httpx.AsyncClient]:
    settings = get_settings()
    return [
        httpx.AsyncClient(
            base_url=settings.paddleocr_base_url,
            timeout=VLM_TIMEOUT,
            limits=httpx.Limits(
                max_connections=VLM_CONN_PER_CLIENT,
                max_keepalive_connections=VLM_CONN_PER_CLIENT,
                keepalive_expiry=VLM_KEEPALIVE_EXPIRY,
            ),
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


async def _ask(payload: bytes, prompt: str) -> tuple[str, str]:
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
    choice = response.json()["choices"][0]
    return str(choice["message"]["content"]), str(choice["finish_reason"])


LOOP_MIN_CHARS = 1500
LOOP_SHINGLE = 32
LOOP_STRIDE = 16
LOOP_DISTINCT_RATIO = 0.35


def _distinct_ratio(text: str) -> float:
    shingles = [text[i : i + LOOP_SHINGLE] for i in range(0, len(text) - LOOP_SHINGLE, LOOP_STRIDE)]
    return len(set(shingles)) / len(shingles) if shingles else 1.0


def is_looping(text: str) -> bool:
    if len(text) < LOOP_MIN_CHARS:
        return False
    return _distinct_ratio(text) < LOOP_DISTINCT_RATIO


def salvage_prefix(text: str) -> str:
    if not is_looping(text):
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if _distinct_ratio(text[:mid]) < LOOP_DISTINCT_RATIO:
            high = mid - 1
        else:
            low = mid
    return text[:low]


TABLE_LOOP_MIN_ROWS = 4
TABLE_LOOP_DISTINCT_RATIO = 0.5


def _table_degenerate(content: str) -> bool:
    rows = [tuple(cells) for cells, header, _ in otsl_rows(content) if not header]
    if len(rows) < TABLE_LOOP_MIN_ROWS:
        return False
    return len(set(rows)) / len(rows) < TABLE_LOOP_DISTINCT_RATIO


def _ran_away(content: str, finish: str, prompt: str) -> bool:
    if finish == "length":
        return True
    if prompt == PROMPT_TABLE:
        return _table_degenerate(content)
    return is_looping(content)


async def recognize(payload: bytes, prompt: str) -> str:
    content, finish = await _ask(payload, prompt)
    if not _ran_away(content, finish, prompt):
        return content.strip()
    if prompt == PROMPT_OCR:
        kept = salvage_prefix(content)
        logger.warning("runaway generation on the plain read prompt, chars=%d — salvaged %d", len(content), len(kept))
        return kept.strip()
    retry, retry_finish = await _ask(payload, PROMPT_OCR)
    if _ran_away(retry, retry_finish, PROMPT_OCR):
        kept = salvage_prefix(retry) or salvage_prefix(content)
        logger.warning("runaway generation prompt=%r, and the plain read looped too — salvaged %d", prompt, len(kept))
        return kept.strip()
    logger.info(
        "runaway generation prompt=%r chars=%d — recovered by plain read, chars=%d", prompt, len(content), len(retry)
    )
    return retry.strip()


_LETTER_RUN = re.compile(r"(?<![A-Za-z])([A-Za-z](?: [A-Za-z]){1,}[.,;:!?-]?)(?![A-Za-z])")


def normalize_latex(text: str) -> str:
    return _LETTER_RUN.sub(lambda match: match.group(1).replace(" ", ""), text)


def block_text(label: str, raw: str) -> str:
    return otsl_to_html(raw) if label == "table" else normalize_latex(raw)

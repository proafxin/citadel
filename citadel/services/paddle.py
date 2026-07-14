import html
import io
import itertools
import logging
import math
import re
from base64 import b64encode
from functools import lru_cache

import httpx
from PIL import Image

from citadel.services.detect import DetBlock
from config import PADDLEOCR_MODEL, get_settings

logger = logging.getLogger(__name__)

CROP_CONCURRENCY = 192  # the ONE bottleneck: a crop is the unit of VLM work, and this semaphore caps what is
# outstanding at the model at all (Running + Waiting). it lives HERE, at the model boundary.
#
# DELIBERATELY ABOVE the server's --max-num-seqs (128), which is the opposite of what it used to be. set EQUAL to it,
# the model can never hold a queue: the server reported `Waiting: 0` on essentially every line and `Running` hovering at
# 101-127 — never pinned at 128. so every time a request finished, its sequence slot sat EMPTY until our event loop
# parsed the response, released this semaphore, base64'd the next crop and posted it. that round trip is dead GPU time,
# eighteen thousand times a run, and it is what the missing 8-10% of utilisation is.
# the excess queues INSIDE the model, which is exactly the point: the scheduler refills a freed slot from its own queue
# instead of waiting on us. the old comment justified the equality by saying a queued request "pins a decoded image" —
# that was true of MinerU, whose payload was a bitmap. ours is a PNG (~40KB), so 32 queued crops cost ~1.3MB.
VLM_CLIENTS = 16
VLM_CONN_HEADROOM = 4  # sockets per client as a MULTIPLE of that client's mean share of the crop semaphore. derived,
# never a literal: a socket count is not a concurrency bound and must never become one. httpx blocks a request that
# cannot get a connection INSIDE the post, so it would sit there holding its crop permit and sending nothing, and the
# model would starve with nothing in our logs to say so
VLM_CONN_PER_CLIENT = VLM_CONN_HEADROOM * CROP_CONCURRENCY // VLM_CLIENTS
VLM_TIMEOUT = 600
VLM_MAX_TOKENS = 4096  # the model's context is prompt + completion TOGETHER, so this cannot be the whole 8192 window:
# every request would overflow before it started (measured: a 400, "you requested 8192 output tokens"). a crop's prompt
# is 183 image tokens for a line of prose and ~1.3k for a dense table, and the longest thing the model emits is that
# table at ~800 tokens — so 4096 is several times what any crop needs and still leaves the window room to spare

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


def encode_crop(crop: Image.Image) -> str:
    # the crop arrives at the model as a base64 data URL, and this builds it AT CUT TIME — in the worker thread that is
    # already holding the bitmap — rather than inside the request.
    # it used to be encoded inside _ask, which meant a crop held its semaphore permit while base64-ing 40KB on the EVENT
    # LOOP. the permit is supposed to bound what is in flight AT THE MODEL; every microsecond it also covers our own
    # encoding is a permit the model cannot use. measured: 160 permits held, but only ~124 requests ever at the model —
    # 23% of every permit's life was spent on our side of the wire.
    return "data:image/png;base64," + b64encode(png_bytes(crop)).decode()


async def _ask(payload: str, prompt: str) -> tuple[str, str]:
    # one crop, one request. the payload is already the encoded data URL — the raw pixels died at cut time and so did
    # the base64, so what waits out the queue here is a string we can hand straight to the serializer
    body = {
        "model": PADDLEOCR_MODEL,
        "max_tokens": VLM_MAX_TOKENS,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": payload}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    response = await get_vlm_client().post("/v1/chat/completions", json=body)
    response.raise_for_status()
    choice = response.json()["choices"][0]
    return str(choice["message"]["content"]), str(choice["finish_reason"])


# a loop does not have to exhaust the token window to be a loop. one did not: 7,167 characters on a page of the maths
# book, of which 5,280 were the SAME 60-character fragment repeated eighty-eight times — and it stopped on its own, so
# finish_reason came back "stop" and every guard we had let it through into the index.
# so detect the repetition itself. a looping generation reuses a tiny vocabulary of fragments, and that is measurable
# without knowing what it repeated: shingle the text and count how many shingles are DISTINCT. real prose is nearly all
# distinct; a loop is nearly none.
LOOP_MIN_CHARS = 1500  # below this there is nothing to loop — the mean block is ~300 chars
LOOP_SHINGLE = 32
LOOP_STRIDE = 16
LOOP_DISTINCT_RATIO = 0.35  # measured: real text lands at 0.9+, the loop above at 0.10


def is_looping(text: str) -> bool:
    if len(text) < LOOP_MIN_CHARS:
        return False
    shingles = [text[i : i + LOOP_SHINGLE] for i in range(0, len(text) - LOOP_SHINGLE, LOOP_STRIDE)]
    return bool(shingles) and len(set(shingles)) / len(shingles) < LOOP_DISTINCT_RATIO


def _ran_away(content: str, finish: str, prompt: str) -> bool:
    # a table is legitimately repetitive — its grid is <fcel>...<fcel>...<nl> over and over — so the shingle test would
    # accuse an honest one. tables are checked by the grid parser instead; here only the token cap applies to them
    if finish == "length":
        return True
    return prompt != PROMPT_TABLE and is_looping(content)


async def recognize(payload: str, prompt: str) -> str:
    content, finish = await _ask(payload, prompt)
    if not _ran_away(content, finish, prompt):
        return content.strip()
    # the region was NOT read — it looped. what exhausts the window is a big matrix under the formula prompt, where the
    # model tries to rebuild the grid as \begin{array}{cccc...} and gets stuck emitting `&.&.&.` until it runs out.
    # the prompt is the cause, and reading is the cure: measured over every runaway in the corpus, the plain OCR prompt
    # recovered SIX OF SIX — clean output every time, 117 to 1539 real characters where the formula prompt returned
    # 12,000 characters of nothing. a repetition penalty fixed only four of six and made one strictly worse, and it
    # would have changed decoding for all eighteen thousand crops to repair eleven.
    if prompt == PROMPT_OCR:
        logger.warning("runaway generation on the plain read prompt, chars=%d — dropped", len(content))
        return ""
    retry, retry_finish = await _ask(payload, PROMPT_OCR)
    if _ran_away(retry, retry_finish, PROMPT_OCR):
        logger.warning("runaway generation prompt=%r, and the plain read looped too — dropped", prompt)
        return ""
    logger.info(
        "runaway generation prompt=%r chars=%d — recovered by plain read, chars=%d", prompt, len(content), len(retry)
    )
    return retry.strip()


# a LaTeX-OCR model emits upright text one glyph at a time, so a word comes back with a space between EVERY letter:
# \mathrm{S E L E C T}, \operatorname*{s t r e a m}, \mathrm{c o n f i d e n c e}, {s t a t e}({n o w}).
# it renders identically and means the same thing, so it is not wrong — but the word `confidence` then does not EXIST
# in the text, only `c o n f i d e n c e` does, and no lexical search and no embedding can find it. rejoining them is
# the same class of normalisation the prose path already does (unicode, hyphen-splits, whitespace), and it is what
# makes a formula findable by its own variable names.
#
# a RUN of single letters, wherever it sits — not a whole group of them. requiring the entire group to be single letters
# missed every German abbreviation in the number-theory books, because the run ends in punctuation: \mathrm{b z w.} is
# "b", "z", "w." — that last part is two characters, so the whole group was skipped. it also missed \mathrm{Aus d e r},
# where one word arrived already joined and the rest stayed spaced.
#
# safe anywhere in the expression because LaTeX math mode IGNORES whitespace — {s t a t e} and {state} render the same.
# the run must be LETTERS, so an operator or a digit ends it: `a + b` and `1 - z` come through untouched. `~` is a LaTeX
# space and not a run separator, so \mathrm{F R O M~r e s t a u r a n t s} becomes \mathrm{FROM~restaurants}: two words.
_LETTER_RUN = re.compile(r"(?<![A-Za-z])([A-Za-z](?: [A-Za-z]){1,}[.,;:!?-]?)(?![A-Za-z])")


def normalize_latex(text: str) -> str:
    return _LETTER_RUN.sub(lambda match: match.group(1).replace(" ", ""), text)


def block_text(label: str, raw: str) -> str:
    # tables arrive as OTSL and every consumer downstream reads HTML; everything else is already its final form —
    # prose as prose, mathematics as LaTeX. the de-spacing runs on ALL of it, not just formula regions: the plain read
    # prompt returns inline mathematics as LaTeX too, so a prose block carries the same artifact
    return otsl_to_html(raw) if label == "table" else normalize_latex(raw)

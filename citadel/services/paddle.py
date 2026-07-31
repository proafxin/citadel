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

CROP_CONCURRENCY = 128  # the ONE bottleneck: a crop is the unit of VLM work, and this semaphore caps what is
# outstanding at the model at all (Running + Waiting). it lives HERE, at the model boundary, and it IS the GPU's batch.
# MUST equal paddle's --max-num-seqs in compose.yaml.
#
# 128 is the MEASURED optimum, and it has now been measured TWICE on two different work profiles. resweep after region
# packing changed the shape of the work (21.6k small crops -> 18.7k larger ones, 6,374 -> 5,952 tok/s):
#      96 -> 475.0s   39.3 crops/s
#     128 -> 472.8s   39.5 crops/s   <- the knee (and exactly the server's --max-num-seqs)
#     160 -> 491.0s   38.0 crops/s
# the earlier curve on the pre-packing profile agreed: 64 -> 495.1s, 128 -> 480.3s, 160 -> 492.9s, 192 -> 520.2s.
# 96 and 128 are inside run-to-run noise (0.9%) of each other; 160 is 3.8% worse, outside it.
#
# GOING UP does not feed the model, and at 160 the server says so directly: `Running: 92, Waiting: 63` at 19.7% KV.
# it admitted 92 and queued the other 63 INSIDE the model, where each pins a decoded image and sends nothing. so the
# excess is not batch, it is a second queue behind the one we already have. NOT a cache limit — zero preemptions, and
# a crop emits ~70 tokens, so KV never came close. the card is power-capped at 1837 of 3090 MHz: its compute budget is
# fixed, and a deeper batch can only cost — attention over more sequences, memory-bandwidth contention.
#
# GOING DOWN buys nothing either, because throughput is concurrency/latency and both fall together. 96 delivers 39.3
# crops/s against 128's 39.5. flat.
#
# utilisation is a TIME metric and on a power-limited card it is not actionable. do not tune against it.

VLM_KEEPALIVE_EXPIRY = 2.0  # seconds an idle pooled connection may live before WE discard it. it MUST be below the
# server's uvicorn timeout_keep_alive (5s): the two defaults are BOTH 5s, and that tie is the disconnect. when a
# connection has been idle ~5s both sides close it at the same instant — the server closes it cleanly (no error, no log)
# and our pool hands the same socket to the next crop, whose write lands on a half-closed connection and reads back
# nothing (httpx RemoteProtocolError, "Server disconnected without sending a response"). measured: every one of 239,207
# server requests returned 200, so the server never failed a request — the failure is purely this reuse race, and it
# only bit in the FIFO lulls of the 809-page book where a pooled socket could sit idle past 5s. expiring OUR side at 2s
# means any connection we reuse has been idle under 2s, so the server (holding to 5s) has not touched it. the window is
# gone, not retried. this is client-side and deterministic — it does NOT depend on the server's timeout, only on being
# safely below it
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
# text it can already read exactly. these are the prose regions that have real characters behind them.
# PAGE FURNITURE BELONGS HERE TOO, and its absence was costing both time and accuracy: a header and a page number were
# cropped and sent on EVERY digital page to re-read characters the layer already held exactly. measured on a 491-page
# technical book, 725 of its 1,101 crops were furniture — ~1.5 a page, and roughly 1,900 crops across the corpus.
# the model reads prose at 98.1%; the layer is exact, so this is strictly more accurate as well as cheaper. a header
# that genuinely has no characters behind it (a scanned letterhead on an otherwise digital page) comes back empty and
# recover_fillin_blocks re-reads it with the model, which is the safety net that makes this safe to widen
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
    if "formula" in label and label != "formula_number":  # formula_number is "(21)" — prose, not mathematics
        return PROMPT_FORMULA
    return PROMPT_OCR


def is_readable(block: DetBlock) -> bool:
    # inline_formula is dropped, not skipped-with-a-hole: it lies INSIDE a text region, and the OCR prompt on that
    # region already returns the mathematics inline as LaTeX. cropping it separately would emit the same content twice
    return block.label not in PICTURE_LABELS and block.label != "inline_formula"


# MEASUREMENT. prefill is billed in image TOKENS, not in pixels we transmit, and the two are not interchangeable:
# the model's own processor resizes into the same [MIN_PIXELS, MAX_PIXELS] window we do, so shrinking our floor
# changes the bytes on the wire and not one token. one token covers PATCH_PIXELS, so the model's floor is a fixed
# toll per crop — charged whether the region fills it or not. what this measures is PACKING HEADROOM: the share of
# the bill that is toll rather than content, which is what merging adjacent regions into one crop could recover.
# the buckets say how far below the toll the regions sit, i.e. how many would fit in a single crop's worth of it
PATCH_PIXELS = 14 * 14 * 2 * 2  # patch_size 14, merge_size 2 — from the model's own preprocessor_config
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
    get_crop_sizes.cache_clear()  # per-RUN figures: without this a second ingestion in the same worker reports the
    # sum of both and every comparison silently reads the wrong denominator


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


# ---- OTSL → HTML -------------------------------------------------------------------------------------
# the model emits tables in OTSL, a token-efficient grid language: one token per cell rather than a styled <td>. every
# consumer downstream (grid extraction, header detection, the canonical Table) is written against HTML, so translate
# here and nothing else has to change. <fcel> full cell, <ecel> empty cell, <lcel>/<ucel>/<xcel> continue a span from
# the left / above / both, <nl> ends a row.
_OTSL_TOKEN = re.compile(r"<(fcel|ecel|lcel|ucel|xcel|nl|ched|rhed|srow)>")


def otsl_rows(otsl: str) -> list[tuple[list[str], bool, bool]]:
    # each row with the model's own verdict on whether it is a COLUMN HEADER, plus whether the row is a SINGLE cell
    # spanning its own full width. <ched> is the header verdict and it is the header signal we no longer have to
    # infer: it travels out as <th> so nothing downstream has to guess. <rhed> marks a row-label cell and <srow> a
    # section row — neither makes the row a column header, so neither sets the flag.
    # the span flag is sourced from the token stream itself — one <fcel> followed only by <lcel> to the row's end —
    # not re-inferred from the flattened text later: a genuine multi-level header's top label spans only PART of a
    # row and still needs its value repeated in every cell it covers (materialize combines it with its sub-header
    # per column), so only a span that consumes the ENTIRE row is flagged here for collapsing downstream
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
            case "lcel":  # spans expanded, never left blank: a flattened span repeats its value in every cell it covers
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


def _row_html(cells: list[str], width: int, tag: str) -> str:
    body = "".join(f"<{tag}>{html.escape(cells[i]) if i < len(cells) else ''}</{tag}>" for i in range(width))
    return f"<tr>{body}</tr>"


def _blank_full_width_spans(rows: list[tuple[list[str], bool, bool]], width: int) -> list[tuple[list[str], bool]]:
    # a row that is ONE cell spanning its own full width is a title/banner, not a repeated header or repeated data —
    # collapse it to its first cell so downstream title/header detection can recognize it as sparse
    out: list[tuple[list[str], bool]] = []
    for cells, header, span in rows:
        collapsed = [cells[0], *([""] * (len(cells) - 1))] if span and len(cells) == width else cells
        out.append((collapsed, header))
    return out


def otsl_to_html(otsl: str) -> str:
    # rows stay in source order and a header row is emitted as <th> IN PLACE — never hoisted into a <thead>, which
    # would reorder a table whose header sits mid-grid. the leading run of all-<th> rows is what the header count reads
    rows = otsl_rows(otsl)
    if not rows:
        return ""
    width = max(len(cells) for cells, _, _ in rows)
    collapsed = _blank_full_width_spans(rows, width)
    body = "".join(_row_html(cells, width, "th" if flag else "td") for cells, flag in collapsed)
    return f"<table>{body}</table>"


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
    # one crop, one request. the payload is PNG BYTES, and the base64 is built here rather than at cut time.
    # it was briefly built at cut time, to keep the encode off the semaphore permit — the idea being that a permit
    # should bound what is in flight AT THE MODEL, not what we are still preparing. that was in service of a starvation
    # theory the data killed: the model was never starved, a deeper queue measured SLOWER, and the encode was never on
    # the critical path. what it DID do was make every crop alive carry a 53KB string instead of 40KB of bytes.
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


def _distinct_ratio(text: str) -> float:
    shingles = [text[i : i + LOOP_SHINGLE] for i in range(0, len(text) - LOOP_SHINGLE, LOOP_STRIDE)]
    return len(set(shingles)) / len(shingles) if shingles else 1.0


def is_looping(text: str) -> bool:
    if len(text) < LOOP_MIN_CHARS:
        return False
    return _distinct_ratio(text) < LOOP_DISTINCT_RATIO


def salvage_prefix(text: str) -> str:
    # a loop is a SUFFIX: the model reads the region, then degenerates and repeats until it stops or hits the cap. the
    # prefix before that is real output, and dropping the whole generation throws it away — one drop this run carried
    # 8,196 characters of which only the tail was garbage. binary search the longest prefix the loop test accepts.
    # used ONLY where the alternative is returning nothing, so it can never be worse than what it replaces
    # the ratio is tested DIRECTLY here, never through is_looping: that treats anything under LOOP_MIN_CHARS as clean,
    # so a generation that looped from its first character would "salvage" a full 1,499 characters of garbage — worse
    # than the drop it replaces
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


# a table response can degenerate without ever hitting the token cap or tripping the shingle test above — OTSL's own
# grid syntax repeats tokens legitimately, which is why tables are exempted from that test, but a real table's ROWS
# still vary: an actual record is rarely byte-identical to the one before it. a small cycle of rows repeating through
# the whole response is the same failure as a looping plain read, just one level up — at the row instead of the
# character. no larger corpus of failures exists yet to tune this against (this is one observed case: seven data
# rows, all identical, distinct ratio 0.14), so the threshold is set conservatively rather than measured
TABLE_LOOP_MIN_ROWS = 4
TABLE_LOOP_DISTINCT_RATIO = 0.5


def _table_degenerate(content: str) -> bool:
    rows = [tuple(cells) for cells, header, _ in otsl_rows(content) if not header]
    if len(rows) < TABLE_LOOP_MIN_ROWS:
        return False
    return len(set(rows)) / len(rows) < TABLE_LOOP_DISTINCT_RATIO


def _ran_away(content: str, finish: str, prompt: str) -> bool:
    # a table is legitimately repetitive at the CHARACTER level — its grid is <fcel>...<fcel>...<nl> over and over —
    # so the shingle test above would accuse an honest one and is skipped for it. that is not the same as exempting
    # tables from degeneracy entirely: _table_degenerate checks the parsed ROWS instead, which a real table still
    # varies even when its raw token stream does not
    if finish == "length":
        return True
    if prompt == PROMPT_TABLE:
        return _table_degenerate(content)
    return is_looping(content)


async def recognize(payload: bytes, prompt: str) -> str:
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
        kept = salvage_prefix(content)
        logger.warning("runaway generation on the plain read prompt, chars=%d — salvaged %d", len(content), len(kept))
        return kept.strip()
    retry, retry_finish = await _ask(payload, PROMPT_OCR)
    if _ran_away(retry, retry_finish, PROMPT_OCR):
        kept = salvage_prefix(retry) or salvage_prefix(content)  # whichever attempt got further before degenerating
        logger.warning("runaway generation prompt=%r, and the plain read looped too — salvaged %d", prompt, len(kept))
        return kept.strip()
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

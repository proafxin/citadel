import re
import unicodedata

from citadel.schemas.content import Block
from citadel.schemas.tree import ContentBlock
from citadel.services.grid import is_math_text

# the block types are the detector's own 25 classes, kept as it labels them rather than flattened into something
# coarser — it separates a document title from a section title, a displayed formula from an inline one, a figure
# caption from body text, and every one of those distinctions is information we would otherwise throw away.
# this maps each to the KIND of leaf it becomes; anything unlisted is prose.
_KIND_BY_TYPE = {
    "paragraph_title": "heading",
    "reference": "heading",
    "algorithm": "code",
    "display_formula": "equation",
    "table": "table",
}

_LIST_MARKER = re.compile(r"^\s*(?:[●○•▪◦‣·*]\s|[-–—]\s|\(?\d{1,3}[.)]\s|\(?[A-Za-z][.)]\s|\([A-Za-z0-9]+\)\s)")
_BULLET_GLYPH = re.compile(r"^\s*[●○•▪◦‣·*\-–—]\s+")
_HEADING_SPLIT = re.compile(r'(?<=["”:.?)])\s*\n\s*')

# page furniture: it recurs on every page and belongs to the page, not to the document. `number` is the detector's
# label for a page number. a `footnote` is NOT furniture — on the math book the footnotes carry real mathematics
PARATEXT_TYPES = {"header", "footer", "number", "aside_text", "header_image", "footer_image"}
PAGE_NUMBER_TYPES = {"number"}
EMPTY_IMAGE_TYPES = {"image", "header_image", "footer_image", "seal", "chart"}

MIN_PARATEXT_PAGES = 2  # paratext RECURS: a running header appears on many pages, a mis-typed body block appears once
_DIGITS = re.compile(r"\d+")
_WHITESPACE = re.compile(r"\s+")


MIN_LOOP_SEGMENT = 40  # chars; below this, repetition is legitimate (a bullet glyph, a short label, an axis tick)
MIN_LOOP_REPEATS = 3  # a real block never emits the SAME 40+ char segment this many times — that is a VLM output loop
_SEGMENT_SPLIT = re.compile(r"\\\\|\n")


def collapse_loops(text: str) -> str:
    # a VLM that loses coherence (rotated spread, dense math) emits the same segment over and over until it hits its
    # token limit. keep the FIRST occurrence of each repeated long segment and drop the copies: the distinct content
    # survives in place, and a 25k-char loop collapses to what was actually read. never drops a one-off segment.
    segments = _SEGMENT_SPLIT.split(text)
    counts: dict[str, int] = {}
    for segment in segments:
        stripped = segment.strip()
        if len(stripped) >= MIN_LOOP_SEGMENT:
            counts[stripped] = counts.get(stripped, 0) + 1
    looped = {segment for segment, count in counts.items() if count >= MIN_LOOP_REPEATS}
    if not looped:
        return text
    kept: list[str] = []
    seen: set[str] = set()
    for segment in segments:
        stripped = segment.strip()
        if stripped in looped:
            if stripped in seen:
                continue
            seen.add(stripped)
        kept.append(segment)
    return "\n".join(kept)


def paratext_key(text: str) -> str:
    # page numbers vary per page, so strip digits: "Chapter 3 ... 14" and "Chapter 3 ... 15" are the SAME running header
    return _WHITESPACE.sub(" ", _DIGITS.sub("", text)).strip().lower()


def _recurring_paratext(blocks: list[Block]) -> set[str]:
    pages: dict[str, set[int]] = {}
    for block in blocks:
        if block.type not in PARATEXT_TYPES or block.type in PAGE_NUMBER_TYPES:
            continue
        key = paratext_key(block.text or "")
        if key:
            pages.setdefault(key, set()).add(block.page_idx)
    return {key for key, seen in pages.items() if len(seen) >= MIN_PARATEXT_PAGES}


def _rescued_type(text: str) -> str:
    # a block mis-typed as header/footer is content: keep it in the tree, and if it is mathematics type it as such so it
    # lands as an equation node in its proper place rather than being smeared into every other node's search text
    return "equation" if is_math_text(text) else "text"


def split_paratext(blocks: list[Block]) -> tuple[list[Block], list[str]]:
    # MinerU types a block per page and cannot see recurrence; we hold the whole document. so its paratext label is a
    # HYPOTHESIS: accept it only when the text actually repeats across pages. a one-off block it called a header is
    # content it mis-typed (a displayed formula near the page edge) — rescue it into the tree at its own position.
    recurring = _recurring_paratext(blocks)
    content: list[Block] = []
    paratext: dict[str, str] = {}  # recurrence key -> first raw text, so a header is recorded ONCE, not once per page
    for block in blocks:
        text = (block.text or "").strip()
        if block.type in PARATEXT_TYPES:
            key = paratext_key(text)
            if not text or block.type in PAGE_NUMBER_TYPES or not key:
                continue
            if key in recurring:
                paratext.setdefault(key, text)
                continue
            content.append(block.model_copy(update={"type": _rescued_type(text), "text": text}))
            continue
        if block.type in EMPTY_IMAGE_TYPES and not text:
            continue
        content.append(block)
    return content, list(paratext.values())


def detail_kind(block_type: str) -> str:
    return _KIND_BY_TYPE.get(block_type, "paragraph")


def _flatten(text: str | None) -> str:
    return re.sub(r"\s*[\r\n]+\s*", " ", text or "").strip()


def _normalize_newlines(text: str | None) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def is_list_item(block: Block) -> bool:
    text = (block.text or "").strip()
    if not text:
        return False
    kind = detail_kind(block.type)
    if kind == "list":
        return True
    return kind == "paragraph" and _LIST_MARKER.match(text) is not None


def split_heading(text: str) -> list[str]:
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    parts = [re.sub(r"\s*\n\s*", " ", part).strip() for part in _HEADING_SPLIT.split(raw)]
    parts = [part for part in parts if part]
    return parts or [""]


def _next_ordinal(counters: dict[int, int], page_no: int) -> int:
    ordinal = counters.get(page_no, 0) + 1
    counters[page_no] = ordinal
    return ordinal


def _push(stack: list[tuple[int, str]], level: int, heading: str) -> None:
    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, heading))


def _path(stack: list[tuple[int, str]]) -> list[str]:
    return [heading for _, heading in stack]


def _heading(stack: list[tuple[int, str]]) -> str | None:
    return stack[-1][1] if stack else None


def _list_block(
    blocks: list[Block], idx: int, stack: list[tuple[int, str]], counters: dict[int, int]
) -> tuple[ContentBlock, int]:
    page_idx = blocks[idx].page_idx
    bbox = blocks[idx].bbox
    items: list[dict] = []
    while idx < len(blocks) and blocks[idx].page_idx == page_idx and is_list_item(blocks[idx]):
        block = blocks[idx]
        content = _BULLET_GLYPH.sub("", (block.text or "").strip(), count=1)
        items.append({"ordinal": len(items), "content": _flatten(content), "depth": block.text_level or 0})
        idx += 1
    page_no = page_idx + 1
    built = ContentBlock(
        heading=_heading(stack),
        heading_path=_path(stack),
        ordinal=_next_ordinal(counters, page_no),
        page_no=page_no,
        type="list",
        kind="list",
        bbox=bbox,
        items=items,
    )
    return built, idx


def _push_heading(block: Block, stack: list[tuple[int, str]]) -> None:
    # a heading is not content: it sets the section every following block belongs to and is never stored on its own
    for piece in split_heading(block.text or ""):
        _push(stack, block.text_level or 1, piece)


def _content_block(block: Block, stack: list[tuple[int, str]], counters: dict[int, int]) -> ContentBlock:
    page_no = block.page_idx + 1
    kind = detail_kind(block.type)
    built = ContentBlock(
        heading=_heading(stack),
        heading_path=_path(stack),
        ordinal=_next_ordinal(counters, page_no),
        page_no=page_no,
        type=block.type,
        kind=kind,
        bbox=block.bbox,
    )
    match kind:
        case "equation":
            built.latex = _normalize_newlines(block.text)
        case "code":
            built.text = _normalize_newlines(block.text)
        case _:
            built.text = _flatten(block.text)
    return built


def _table_blocks(
    block: Block, stack: list[tuple[int, str]], counters: dict[int, int], count: int
) -> list[ContentBlock]:
    page_no = block.page_idx + 1
    return [
        ContentBlock(
            heading=_heading(stack),
            heading_path=_path(stack),
            ordinal=_next_ordinal(counters, page_no),
            page_no=page_no,
            type="table",
            kind="table",
            bbox=block.bbox,
        )
        for _ in range(count)
    ]


DOC_TITLE_TYPE = "doc_title"


def split_title(blocks: list[Block]) -> tuple[str | None, list[Block]]:
    title = next((_flatten(block.text) for block in blocks if block.type == DOC_TITLE_TYPE and block.text), None)
    return title, [block for block in blocks if block.type != DOC_TITLE_TYPE]


def build_tree(blocks: list[Block], table_counts: dict[int, int]) -> list[ContentBlock]:
    built: list[ContentBlock] = []
    stack: list[tuple[int, str]] = []
    counters: dict[int, int] = {}
    idx = 0
    while idx < len(blocks):
        block = blocks[idx]
        kind = detail_kind(block.type)
        if is_list_item(block):
            item, idx = _list_block(blocks, idx, stack, counters)
            built.append(item)
        elif kind == "list":
            idx += 1
        elif kind == "heading":
            _push_heading(block, stack)
            idx += 1
        elif kind == "table":
            built.extend(_table_blocks(block, stack, counters, table_counts.get(idx, 1)))
            idx += 1
        else:
            built.append(_content_block(block, stack, counters))
            idx += 1
    return built


def _leaf_text(block: ContentBlock) -> str:
    if block.kind == "equation":
        return block.latex or ""
    if block.kind == "list":
        # the item's position is content: "the second item" is only answerable if the ordinal survives flattening
        return " ".join(f"{int(item.get('ordinal', 0)) + 1}. {item.get('content', '')}" for item in block.items or [])
    return block.text or ""


def build_raw(block: ContentBlock) -> dict | None:
    # the block's own content in its source shape — what markdown is rebuilt from. a table's cells are NOT here: they
    # live in table_rows because they are queried by SQL
    match block.kind:
        case "equation":
            return {"latex": block.latex or ""}
        case "code":
            return {"text": block.text or ""}
        case "list":
            return {"items": block.items or []}
        case "table":
            return None
        case _:
            return {"text": block.text or ""}


def _clean_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"(?<=[a-z])-\s+(?=[a-z])", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_name(name: str) -> str:
    name = unicodedata.normalize("NFC", name)
    return re.sub(r"\s+", " ", re.sub(r"[_-]+", " ", name)).strip()


def build_table_search_text(
    library_name: str,
    filename: str,
    sheet: str,
    title: str | None,
    caption: str | None,
    notes: list[str],
    headers: list[str],
) -> str:
    fields = [sheet, title or "", caption or "", *notes, *headers]
    parts = [
        _clean_name(library_name),
        _clean_name(filename.rsplit(".", 1)[0] if "." in filename else filename),
        *(_clean_text(field) for field in fields),
    ]
    return "\n".join(part for part in parts if part)


EQUATION_CONTEXT_BACK = 3  # blocks to look back for the sentence that introduces an equation


def _introducing_prose(blocks: list[ContentBlock], index: int) -> str:
    # an equation is unsearchable on its own. LaTeX has no natural-language surface: nobody asks a question in
    # \sum_{n=1}^{\infty}, they ask for "the sum of the reciprocals of the squares" — and those words are sitting in the
    # prose that INTRODUCES the equation, one or two blocks above it ("Theorem 3.1 states that..."). that sentence is
    # already in the tree; it was simply never part of the equation's own search text. same page only: a sentence from
    # the previous page is not introducing anything.
    page = blocks[index].page_no
    for block in reversed(blocks[max(index - EQUATION_CONTEXT_BACK, 0) : index]):
        if block.page_no != page:
            break
        if block.kind == "paragraph" and (block.text or "").strip():
            return block.text or ""
    return ""


def build_search_text(blocks: list[ContentBlock], library_name: str, filename: str) -> dict[int, str]:
    # paratext is DELIBERATELY absent: a running header repeated into every node's search text makes every embedding on
    # the page share an identical block of tokens, which destroys discrimination. it lives in document metadata instead.
    result: dict[int, str] = {}
    for index, block in enumerate(blocks):
        if block.kind == "table":  # a table's search text is built from its schema, not its cells
            continue
        context = _introducing_prose(blocks, index) if block.kind == "equation" else ""
        parts = [
            _clean_name(library_name),
            _clean_name(filename.rsplit(".", 1)[0] if "." in filename else filename),
            *(_clean_text(text) for text in block.heading_path),
            _clean_text(context),
            _clean_text(_leaf_text(block)),
        ]
        result[index] = "\n".join(part for part in parts if part)
    return result

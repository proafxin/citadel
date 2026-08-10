import re
import unicodedata

from citadel.schemas.content import Block
from citadel.schemas.tree import ContentBlock
from citadel.services.grid import is_math_text

_KIND_BY_TYPE = {
    "paragraph_title": "heading",
    "reference": "heading",
    "title": "heading",
    "algorithm": "code",
    "code": "code",
    "list_item": "list",
    "display_formula": "equation",
    "equation": "equation",
    "table": "table",
}

_LIST_MARKER = re.compile(r"^\s*(?:[●○•▪◦‣·*]\s|[-–—]\s|\(?\d{1,3}[.)]\s|\(?[A-Za-z][.)]\s|\([A-Za-z0-9]+\)\s)")
_BULLET_GLYPH = re.compile(r"^\s*[●○•▪◦‣·*\-–—]\s+")
_HEADING_SPLIT = re.compile(r'(?<=["”:.?)])\s*\n\s*')

PARATEXT_TYPES = {"header", "footer", "number", "aside_text", "header_image", "footer_image"}
PAGE_NUMBER_TYPES = {"number"}
EMPTY_IMAGE_TYPES = {"image", "header_image", "footer_image", "seal", "chart"}

MIN_PARATEXT_PAGES = 2
_DIGITS = re.compile(r"\d+")
_WHITESPACE = re.compile(r"\s+")


MIN_LOOP_SEGMENT = 40
MIN_LOOP_REPEATS = 3
_SEGMENT_SPLIT = re.compile(r"\\\\|\n")


def collapse_loops(text: str) -> str:
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
    return "equation" if is_math_text(text) else "text"


def split_paratext(blocks: list[Block]) -> tuple[list[Block], list[str]]:
    recurring = _recurring_paratext(blocks)
    content: list[Block] = []
    paratext: dict[str, str] = {}
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
        return " ".join(f"{int(item.get('ordinal', 0)) + 1}. {item.get('content', '')}" for item in block.items or [])
    return block.text or ""


def build_raw(block: ContentBlock) -> dict | None:
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


EQUATION_CONTEXT_BACK = 3


def _introducing_prose(blocks: list[ContentBlock], index: int) -> str:
    page = blocks[index].page_no
    for block in reversed(blocks[max(index - EQUATION_CONTEXT_BACK, 0) : index]):
        if block.page_no != page:
            break
        if block.kind == "paragraph" and (block.text or "").strip():
            return block.text or ""
    return ""


def build_search_text(blocks: list[ContentBlock], library_name: str, filename: str) -> dict[int, str]:
    result: dict[int, str] = {}
    for index, block in enumerate(blocks):
        if block.kind == "table":
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

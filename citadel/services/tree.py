import re
import unicodedata

from citadel.schemas.content import Block
from citadel.schemas.tree import NodeSpec
from citadel.services.grid import is_math_text

_KIND_BY_TYPE = {
    "title": "heading",
    "code": "code",
    "algorithm": "code",
    "equation": "equation",
    "equation_block": "equation",
    "list": "list",
    "list_item": "list",
    "table": "table",
}

_LIST_MARKER = re.compile(r"^\s*(?:[●○•▪◦‣·*]\s|[-–—]\s|\(?\d{1,3}[.)]\s|\(?[A-Za-z][.)]\s|\([A-Za-z0-9]+\)\s)")
_BULLET_GLYPH = re.compile(r"^\s*[●○•▪◦‣·*\-–—]\s+")
_HEADING_SPLIT = re.compile(r'(?<=["”:.?)])\s*\n\s*')

PARATEXT_TYPES = {"header", "footer", "page_number", "page_footnote", "aside_text"}
PAGE_NUMBER_TYPES = {"page_number"}
EMPTY_IMAGE_TYPES = {"image", "image_block"}

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


def make_content_id(library_id: int, doc_id: int, page_no: int, ordinal: int) -> str:
    return f"{library_id}_{doc_id}_{page_no}_{ordinal}"


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


def _push(stack: list[tuple[int, str]], level: int, content_id: str) -> str | None:
    while stack and stack[-1][0] >= level:
        stack.pop()
    parent = stack[-1][1] if stack else None
    stack.append((level, content_id))
    return parent


def _list_node(
    blocks: list[Block], idx: int, library_id: int, doc_id: int, stack: list[tuple[int, str]], counters: dict[int, int]
) -> tuple[NodeSpec, int]:
    page_idx = blocks[idx].page_idx
    bbox = blocks[idx].bbox
    items: list[dict] = []
    while idx < len(blocks) and blocks[idx].page_idx == page_idx and is_list_item(blocks[idx]):
        block = blocks[idx]
        content = _BULLET_GLYPH.sub("", (block.text or "").strip(), count=1)
        items.append({"ordinal": len(items), "content": _flatten(content), "depth": block.text_level or 0})
        idx += 1
    page_no = page_idx + 1
    ordinal = _next_ordinal(counters, page_no)
    spec = NodeSpec(
        content_id=make_content_id(library_id, doc_id, page_no, ordinal),
        parent_content_id=stack[-1][1] if stack else None,
        ordinal=ordinal,
        page_no=page_no,
        type="list",
        kind="list",
        level=None,
        bbox=bbox,
        items=items,
    )
    return spec, idx


def _heading_nodes(
    block: Block,
    library_id: int,
    doc_id: int,
    stack: list[tuple[int, str]],
    counters: dict[int, int],
) -> list[NodeSpec]:
    page_no = block.page_idx + 1
    nodes: list[NodeSpec] = []
    for piece in split_heading(block.text or ""):
        level = block.text_level or 1
        ordinal = _next_ordinal(counters, page_no)
        content_id = make_content_id(library_id, doc_id, page_no, ordinal)
        parent = _push(stack, level, content_id)
        nodes.append(
            NodeSpec(
                content_id=content_id,
                parent_content_id=parent,
                ordinal=ordinal,
                page_no=page_no,
                type="level",
                kind="heading",
                level=level,
                bbox=block.bbox,
                text=piece,
            )
        )
    return nodes


def _content_node(
    block: Block, library_id: int, doc_id: int, stack: list[tuple[int, str]], counters: dict[int, int]
) -> NodeSpec:
    page_no = block.page_idx + 1
    kind = detail_kind(block.type)
    ordinal = _next_ordinal(counters, page_no)
    spec = NodeSpec(
        content_id=make_content_id(library_id, doc_id, page_no, ordinal),
        parent_content_id=stack[-1][1] if stack else None,
        ordinal=ordinal,
        page_no=page_no,
        type=block.type,
        kind=kind,
        level=None,
        bbox=block.bbox,
    )
    match kind:
        case "equation":
            spec.latex = _normalize_newlines(block.text)
        case "code":
            spec.text = _normalize_newlines(block.text)
        case _:
            spec.text = _flatten(block.text)
    return spec


def _table_nodes(
    block: Block, library_id: int, doc_id: int, stack: list[tuple[int, str]], counters: dict[int, int], count: int
) -> list[NodeSpec]:
    page_no = block.page_idx + 1
    parent = stack[-1][1] if stack else None
    nodes: list[NodeSpec] = []
    for _ in range(count):
        ordinal = _next_ordinal(counters, page_no)
        nodes.append(
            NodeSpec(
                content_id=make_content_id(library_id, doc_id, page_no, ordinal),
                parent_content_id=parent,
                ordinal=ordinal,
                page_no=page_no,
                type="table",
                kind="table",
                level=None,
                bbox=block.bbox,
            )
        )
    return nodes


def build_tree(blocks: list[Block], library_id: int, doc_id: int, table_counts: dict[int, int]) -> list[NodeSpec]:
    specs: list[NodeSpec] = []
    stack: list[tuple[int, str]] = []
    counters: dict[int, int] = {}
    idx = 0
    while idx < len(blocks):
        block = blocks[idx]
        kind = detail_kind(block.type)
        if is_list_item(block):
            spec, idx = _list_node(blocks, idx, library_id, doc_id, stack, counters)
            specs.append(spec)
        elif kind == "list":
            idx += 1
        elif kind == "heading":
            specs.extend(_heading_nodes(block, library_id, doc_id, stack, counters))
            idx += 1
        elif kind == "table":
            specs.extend(_table_nodes(block, library_id, doc_id, stack, counters, table_counts.get(idx, 1)))
            idx += 1
        else:
            specs.append(_content_node(block, library_id, doc_id, stack, counters))
            idx += 1
    return specs


def _leaf_text(spec: NodeSpec) -> str:
    if spec.kind == "equation":
        return spec.latex or ""
    if spec.kind == "list":
        return " ".join(item.get("content", "") for item in spec.items or [])
    return spec.text or ""


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
    description: str,
) -> str:
    fields = [sheet, title or "", caption or "", *notes, *headers, description]
    parts = [
        _clean_name(library_name),
        _clean_name(filename.rsplit(".", 1)[0] if "." in filename else filename),
        *(_clean_text(field) for field in fields),
    ]
    return "\n".join(part for part in parts if part)


def build_search_text(specs: list[NodeSpec], library_name: str, filename: str) -> dict[str, str]:
    # paratext is DELIBERATELY absent: a running header repeated into every node's search text makes every embedding on
    # the page share an identical block of tokens, which destroys discrimination. it lives in document metadata instead.
    by_id = {spec.content_id: spec for spec in specs}
    result: dict[str, str] = {}
    for spec in specs:
        if spec.kind in {"heading", "table"}:
            continue
        headings: list[str] = []
        parent = spec.parent_content_id
        while parent is not None:
            ancestor = by_id[parent]
            if ancestor.kind == "heading" and ancestor.text:
                headings.append(ancestor.text)
            parent = ancestor.parent_content_id
        headings.reverse()
        parts = [
            _clean_name(library_name),
            _clean_name(filename.rsplit(".", 1)[0] if "." in filename else filename),
            *(_clean_text(text) for text in headings),
            _clean_text(_leaf_text(spec)),
        ]
        result[spec.content_id] = "\n".join(part for part in parts if part)
    return result

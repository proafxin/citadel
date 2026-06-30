import itertools
import re
from collections.abc import Iterator

from citadel.schemas.content import Block
from citadel.schemas.tree import HeadingInfo, NodeSpec

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
_NUM_DOTTED = re.compile(r"^\s*\d+(?:\.\d+)+")
_NUM_SINGLE = re.compile(r"^\s*\d+\.")
_HEADING_SPLIT = re.compile(r'(?<=["”:.?)])\s*\n\s*')


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


def pattern_level(text: str) -> int:
    stripped = (text or "").strip()
    if not stripped:
        return 1
    letters = [char for char in stripped if char.isalpha()]
    if stripped.upper().startswith("SECTION") or (letters and all(char.isupper() for char in letters)):
        return 1
    dotted = _NUM_DOTTED.match(stripped)
    if dotted:
        return 1 + stripped[: dotted.end()].count(".")
    if _NUM_SINGLE.match(stripped) or stripped.endswith(":"):
        return 2
    return 3


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
        content = _BULLET_GLYPH.sub("", (blocks[idx].text or "").strip(), count=1)
        items.append({"ordinal": len(items), "content": _flatten(content)})
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
    levels: dict[int, int],
    heading_idx: Iterator[int],
) -> list[NodeSpec]:
    page_no = block.page_idx + 1
    nodes: list[NodeSpec] = []
    for piece in split_heading(block.text or ""):
        level = levels.get(next(heading_idx), 1)
        ordinal = _next_ordinal(counters, page_no)
        content_id = make_content_id(library_id, doc_id, page_no, ordinal)
        parent = _push(stack, level, content_id)
        nodes.append(
            NodeSpec(
                content_id=content_id,
                parent_content_id=parent,
                ordinal=ordinal,
                page_no=page_no,
                type=block.type,
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
        case "table":
            spec.table_html = block.text
        case "code":
            spec.text = _normalize_newlines(block.text)
        case _:
            spec.text = _flatten(block.text)
    return spec


def _following_snippet(blocks: list[Block], index: int) -> str:
    for block in blocks[index + 1 :]:
        if detail_kind(block.type) == "heading":
            return ""
        text = (block.text or "").strip()
        if text:
            return text[:150]
    return ""


def heading_infos(blocks: list[Block]) -> list[HeadingInfo]:
    infos: list[HeadingInfo] = []
    for index, block in enumerate(blocks):
        if detail_kind(block.type) != "heading":
            continue
        snippet = _following_snippet(blocks, index)
        infos.extend(
            HeadingInfo(text=piece, font_size=block.font_size, page=block.page_idx + 1, context=snippet)
            for piece in split_heading(block.text or "")
        )
    return infos


def build_tree(
    blocks: list[Block], library_id: int, doc_id: int, heading_levels: dict[int, int] | None = None
) -> list[NodeSpec]:
    levels = heading_levels or {}
    heading_idx = itertools.count()
    specs: list[NodeSpec] = []
    stack: list[tuple[int, str]] = []
    counters: dict[int, int] = {}
    idx = 0
    while idx < len(blocks):
        block = blocks[idx]
        if is_list_item(block):
            spec, idx = _list_node(blocks, idx, library_id, doc_id, stack, counters)
            specs.append(spec)
        elif detail_kind(block.type) == "list":
            idx += 1
        elif detail_kind(block.type) == "heading":
            specs.extend(_heading_nodes(block, library_id, doc_id, stack, counters, levels, heading_idx))
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


def build_search_text(specs: list[NodeSpec], library_name: str, filename: str) -> dict[str, str]:
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
        parts = [library_name, filename, *headings, _leaf_text(spec)]
        result[spec.content_id] = "\n".join(part for part in parts if part)
    return result


def _render_node(node: dict, parts: list[str]) -> None:
    match detail_kind(node["type"]):
        case "heading":
            parts.append(f"{'#' * (node.get('level') or 1)} {node.get('content') or ''}".rstrip())
        case "code":
            parts.append(f"```\n{node.get('content') or ''}\n```")
        case "equation":
            parts.append(f"$$\n{node.get('content') or ''}\n$$")
        case "list":
            parts.append("\n".join(f"- {item.get('content', '')}" for item in node.get("list_items") or []))
        case _:
            if node.get("content"):
                parts.append(node["content"])
    for child in node.get("children", []):
        _render_node(child, parts)


def render_markdown(tree: dict) -> str:
    parts: list[str] = []
    for child in tree.get("children", []):
        _render_node(child, parts)
    return "\n\n".join(parts)

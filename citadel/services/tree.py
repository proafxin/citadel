import re

from citadel.schemas.content import Block
from citadel.schemas.tree import NodeSpec

_HEADING_NUM = re.compile(r"^\s*(\d+(?:\.\d+)*)")

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


def detail_kind(block_type: str) -> str:
    return _KIND_BY_TYPE.get(block_type, "paragraph")


def heading_level(text: str) -> int:
    match = _HEADING_NUM.match(text or "")
    return match.group(1).count(".") + 1 if match else 1


def make_content_id(library_id: int, doc_id: int, page_no: int, ordinal: int) -> str:
    return f"{library_id}_{doc_id}_{page_no}_{ordinal}"


def build_tree(blocks: list[Block], library_id: int, doc_id: int) -> list[NodeSpec]:
    specs: list[NodeSpec] = []
    stack: list[tuple[int, str]] = []
    counters: dict[int, int] = {}
    idx = 0
    total = len(blocks)
    while idx < total:
        block = blocks[idx]
        kind = detail_kind(block.type)
        page_no = block.page_idx + 1
        if kind == "list":
            run: list[Block] = []
            while idx < total and detail_kind(blocks[idx].type) == "list" and blocks[idx].page_idx == block.page_idx:
                if blocks[idx].text:
                    run.append(blocks[idx])
                idx += 1
            ordinal = counters.get(page_no, 0) + 1
            counters[page_no] = ordinal
            specs.append(
                NodeSpec(
                    content_id=make_content_id(library_id, doc_id, page_no, ordinal),
                    parent_content_id=stack[-1][1] if stack else None,
                    ordinal=ordinal,
                    page_no=page_no,
                    type="list",
                    kind="list",
                    level=None,
                    bbox=block.bbox,
                    items=[{"ordinal": i, "content": item.text or ""} for i, item in enumerate(run)],
                )
            )
            continue
        ordinal = counters.get(page_no, 0) + 1
        counters[page_no] = ordinal
        content_id = make_content_id(library_id, doc_id, page_no, ordinal)
        level = heading_level(block.text or "") if kind == "heading" else None
        if level is not None:
            while stack and stack[-1][0] >= level:
                stack.pop()
        spec = NodeSpec(
            content_id=content_id,
            parent_content_id=stack[-1][1] if stack else None,
            ordinal=ordinal,
            page_no=page_no,
            type=block.type,
            kind=kind,
            level=level,
            bbox=block.bbox,
        )
        match kind:
            case "equation":
                spec.latex = block.text
            case "table":
                spec.table_html = block.text
            case _:
                spec.text = block.text
        specs.append(spec)
        if level is not None:
            stack.append((level, content_id))
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

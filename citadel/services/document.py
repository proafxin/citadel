from fastapi import HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from citadel.db import get_sessionmaker
from citadel.models.content import Code, ContentNode, Equation, Heading, ListBlock, Paragraph
from citadel.models.document import Document
from citadel.models.table import Table
from citadel.schemas.content import Block
from citadel.services.tree import NodeSpec, build_tree, detail_kind, render_markdown


async def create_document(library_id: int, filename: str) -> int:
    async with get_sessionmaker()() as session, session.begin():
        document = Document(library_id=library_id, filename=filename, status="pending")
        session.add(document)
        await session.flush()
        return document.id


def _add_detail(session: AsyncSession, spec: NodeSpec, doc_id: int) -> None:
    match spec.kind:
        case "heading":
            session.add(Heading(content_id=spec.content_id, text=spec.text or ""))
        case "code":
            session.add(Code(content_id=spec.content_id, text=spec.text or ""))
        case "equation":
            session.add(Equation(content_id=spec.content_id, latex=spec.latex or ""))
        case "list":
            session.add(ListBlock(content_id=spec.content_id, items=spec.items or []))
        case "table":
            session.add(
                Table(
                    content_id=spec.content_id,
                    document_id=doc_id,
                    origin="pdf",
                    columns=[],
                    table_metadata={"html": spec.table_html or ""},
                    description="",
                    n_rows=0,
                    sample_rows=[],
                )
            )
        case _:
            session.add(Paragraph(content_id=spec.content_id, text=spec.text or ""))


async def save_document_tree(doc_id: int, blocks: list[Block], status: str) -> None:
    async with get_sessionmaker()() as session, session.begin():
        document = await session.get_one(Document, doc_id)
        document.status = status
        id_map: dict[str, int] = {}
        for spec in build_tree(blocks, document.library_id, doc_id):
            parent_id = id_map[spec.parent_content_id] if spec.parent_content_id is not None else None
            node = ContentNode(
                content_id=spec.content_id,
                document_id=doc_id,
                parent_id=parent_id,
                ordinal=spec.ordinal,
                page_no=spec.page_no,
                type=spec.type,
                level=spec.level,
                bbox=spec.bbox,
            )
            session.add(node)
            await session.flush()
            id_map[spec.content_id] = node.id
            _add_detail(session, spec, doc_id)


async def _load_payloads(session: AsyncSession, ids: list[str]) -> dict[str, dict]:
    return {
        "paragraphs": {
            row.content_id: row.text
            for row in await session.scalars(select(Paragraph).where(Paragraph.content_id.in_(ids)))
        },
        "headings": {
            row.content_id: row.text
            for row in await session.scalars(select(Heading).where(Heading.content_id.in_(ids)))
        },
        "codes": {
            row.content_id: row.text
            for row in await session.scalars(select(Code).where(Code.content_id.in_(ids)))
        },
        "equations": {
            row.content_id: row.latex
            for row in await session.scalars(select(Equation).where(Equation.content_id.in_(ids)))
        },
        "lists": {
            row.content_id: row.items
            for row in await session.scalars(select(ListBlock).where(ListBlock.content_id.in_(ids)))
        },
        "tables": {
            row.content_id: row
            for row in await session.scalars(select(Table).where(Table.content_id.in_(ids)))
        },
    }


def _node_dict(node: ContentNode, payloads: dict[str, dict]) -> dict:
    data: dict = {"type": node.type}
    if node.level is not None:
        data["level"] = node.level
    if node.label is not None:
        data["label"] = node.label
    match detail_kind(node.type):
        case "heading":
            data["content"] = payloads["headings"].get(node.content_id)
        case "code":
            data["content"] = payloads["codes"].get(node.content_id)
        case "equation":
            data["content"] = payloads["equations"].get(node.content_id)
        case "list":
            data["list_items"] = payloads["lists"].get(node.content_id)
        case "table":
            table = payloads["tables"].get(node.content_id)
            data["content"] = table.table_metadata.get("html") if table is not None else None
        case _:
            data["content"] = payloads["paragraphs"].get(node.content_id)
    return data


def _build_node(node: ContentNode, children_of: dict[int | None, list[ContentNode]], payloads: dict[str, dict]) -> dict:
    data = _node_dict(node, payloads)
    children = [_build_node(child, children_of, payloads) for child in children_of.get(node.id, [])]
    if children:
        data["children"] = children
    return data


async def build_document_tree(session: AsyncSession, document: Document) -> dict:
    nodes = list(
        await session.scalars(
            select(ContentNode)
            .where(ContentNode.document_id == document.id)
            .order_by(ContentNode.page_no, ContentNode.ordinal)
        )
    )
    payloads = await _load_payloads(session, [node.content_id for node in nodes])
    children_of: dict[int | None, list[ContentNode]] = {}
    for node in nodes:
        children_of.setdefault(node.parent_id, []).append(node)
    roots = [_build_node(node, children_of, payloads) for node in children_of.get(None, [])]
    return {"type": "document", "filename": document.filename, "children": roots}


async def get_document_tree(doc_id: int) -> dict | None:
    async with get_sessionmaker()() as session:
        document = await session.get(Document, doc_id)
        if document is None:
            return None
        return await build_document_tree(session, document)


async def get_result(doc_id: int) -> dict:
    tree = await get_document_tree(doc_id)
    if tree is None:
        raise HTTPException(status_code=404, detail="result not ready")
    return tree


async def get_markdown(doc_id: int) -> PlainTextResponse:
    tree = await get_document_tree(doc_id)
    if tree is None:
        raise HTTPException(status_code=404, detail="result not ready")
    return PlainTextResponse(render_markdown(tree), media_type="text/markdown")

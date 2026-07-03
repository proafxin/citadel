import asyncio
import json

import zstandard
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from citadel.db import get_sessionmaker
from citadel.models.content import Code, ContentNode, Equation, ListBlock, Paragraph
from citadel.models.document import Document
from citadel.models.library import Library
from citadel.models.table import Table, TableRow
from citadel.schemas.content import Block
from citadel.services.excel import MaterializedTable
from citadel.services.tabular import stitch_tables, structure_html_tables
from citadel.services.tree import (
    NodeSpec,
    build_search_text,
    build_table_search_text,
    build_tree,
    detail_kind,
    make_content_id,
    split_paratext,
)
from citadel.storage import delete_object, get_object, put_object


async def create_document(library_id: int, filename: str) -> int:
    async with get_sessionmaker()() as session, session.begin():
        document = Document(library_id=library_id, filename=filename, status="pending")
        session.add(document)
        await session.flush()
        return document.id


async def create_documents(library_id: int, filenames: list[str]) -> list[int]:
    async with get_sessionmaker()() as session, session.begin():
        documents = [Document(library_id=library_id, filename=name, status="pending") for name in filenames]
        session.add_all(documents)
        await session.flush()
        return [document.id for document in documents]


async def notify_embed(library_id: int) -> None:
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(text("SELECT pg_notify('embed', :library)"), {"library": str(library_id)})


async def _maybe_notify_embed(session: AsyncSession, library_id: int) -> None:
    library = await session.get(Library, library_id)
    if library is None or library.tier != "tier_2":
        return
    pending = await session.scalar(
        select(func.count()).select_from(Document).where(Document.library_id == library_id, Document.status == "pending")
    )
    if pending == 0:
        await session.execute(text("SELECT pg_notify('embed', :library)"), {"library": str(library_id)})


def _add_detail(session: AsyncSession, spec: NodeSpec, doc_id: int) -> None:
    match spec.kind:
        case "code":
            session.add(Code(content_id=spec.content_id, text=spec.text or ""))
        case "equation":
            session.add(Equation(content_id=spec.content_id, latex=spec.latex or ""))
        case "list":
            session.add(ListBlock(content_id=spec.content_id, items=spec.items or []))
        case _:
            session.add(Paragraph(content_id=spec.content_id, text=spec.text or ""))


async def _resolve_tables(blocks: list[Block], context: str) -> tuple[dict[int, int], list[MaterializedTable]]:
    counts: dict[int, int] = {}
    queue: list[MaterializedTable] = []
    for idx, block in enumerate(blocks):
        if block.type == "table":
            tables = await structure_html_tables(block.text or "", context)
            counts[idx] = len(tables)
            queue.extend(tables)
    return counts, queue


async def _add_table(session: AsyncSession, content_id: str, doc_id: int, table: MaterializedTable) -> None:
    row = Table(
        content_id=content_id,
        document_id=doc_id,
        columns=[column.model_dump() for column in table.columns],
        table_metadata={"title": table.title, "caption": table.caption, "notes": table.notes},
        description=table.description,
        n_rows=table.n_rows,
        sample_rows=table.sample_rows,
        anchors=table.anchors,
    )
    session.add(row)
    await session.flush()
    session.add_all(TableRow(table_id=row.id, row_idx=index, values=values) for index, values in enumerate(table.rows))


async def save_document_tree(doc_id: int, blocks: list[Block], status: str, ingest_seconds: float | None) -> None:
    async with get_sessionmaker()() as session:
        # idempotent merge: a redelivered/retried merge (worker died after commit, or persist failed post-commit)
        # finds the tree already written and only refreshes status — never re-inserts duplicate content_ids
        if await session.scalar(select(ContentNode.id).where(ContentNode.document_id == doc_id).limit(1)) is not None:
            async with session.begin():
                document = await session.get_one(Document, doc_id)
                document.status = status
                document.ingest_seconds = ingest_seconds
                await _maybe_notify_embed(session, document.library_id)
            return
        document = await session.get_one(Document, doc_id)
        library = await session.get_one(Library, document.library_id)
        library_id, filename, library_name = document.library_id, document.filename, library.name
    content_blocks, paratext = split_paratext(blocks)
    stitched = stitch_tables(content_blocks)
    table_counts, table_queue = await _resolve_tables(stitched, filename)
    specs = list(build_tree(stitched, library_id, doc_id, table_counts))
    search_text = build_search_text(specs, library_name, filename, paratext)
    tables = iter(table_queue)
    async with get_sessionmaker()() as session, session.begin():
        document = await session.get_one(Document, doc_id)
        document.status = status
        document.ingest_seconds = ingest_seconds
        id_map: dict[str, int] = {}
        for spec in specs:
            parent_id = id_map[spec.parent_content_id] if spec.parent_content_id is not None else None
            table = next(tables) if spec.kind == "table" else None
            if table is not None:
                node_search = build_table_search_text(
                    library_name,
                    filename,
                    "",
                    table.title,
                    table.caption,
                    table.notes,
                    [column.header or "" for column in table.columns],
                    table.description,
                    paratext.get(spec.page_no),
                )
            else:
                node_search = search_text.get(spec.content_id)
            node = ContentNode(
                content_id=spec.content_id,
                document_id=doc_id,
                parent_id=parent_id,
                ordinal=spec.ordinal,
                page_no=spec.page_no,
                type=spec.type,
                level=spec.level,
                label=spec.text if spec.kind == "heading" else None,
                bbox=spec.bbox,
                search_text=node_search,
            )
            session.add(node)
            await session.flush()
            id_map[spec.content_id] = node.id
            if table is not None:
                await _add_table(session, spec.content_id, doc_id, table)
            elif spec.kind != "heading":
                _add_detail(session, spec, doc_id)
        await _maybe_notify_embed(session, library_id)


async def save_sheet_tables(
    doc_id: int, sheet_no: int, sheet_name: str, tables: list[tuple[int, MaterializedTable]]
) -> None:
    async with get_sessionmaker()() as session, session.begin():
        existing = await session.scalar(
            select(ContentNode.id).where(ContentNode.document_id == doc_id, ContentNode.sheet_no == sheet_no).limit(1)
        )
        if existing is not None:
            return
        document = await session.get_one(Document, doc_id)
        library = await session.get_one(Library, document.library_id)
        sheet = ContentNode(
            content_id=make_content_id(document.library_id, doc_id, sheet_no, 0),
            document_id=doc_id,
            sheet_no=sheet_no,
            ordinal=0,
            type="level",
            level=1,
            label=sheet_name,
        )
        session.add(sheet)
        await session.flush()
        for ordinal, table in tables:
            content_id = make_content_id(document.library_id, doc_id, sheet_no, ordinal)
            node_search = build_table_search_text(
                library.name,
                document.filename,
                sheet_name,
                table.title,
                table.caption,
                table.notes,
                [column.header or "" for column in table.columns],
                table.description,
            )
            node = ContentNode(
                content_id=content_id,
                document_id=doc_id,
                parent_id=sheet.id,
                sheet_no=sheet_no,
                ordinal=ordinal,
                type="table",
                search_text=node_search,
            )
            session.add(node)
            await session.flush()
            row = Table(
                content_id=content_id,
                document_id=doc_id,
                columns=[column.model_dump() for column in table.columns],
                table_metadata={
                    "sheet": sheet_name,
                    "title": table.title,
                    "caption": table.caption,
                    "notes": table.notes,
                },
                description=table.description,
                n_rows=table.n_rows,
                sample_rows=table.sample_rows,
                anchors=table.anchors,
            )
            session.add(row)
            await session.flush()
            session.add_all(
                TableRow(table_id=row.id, row_idx=index, values=values) for index, values in enumerate(table.rows)
            )


async def finalize_tabular(doc_id: int, ingest_seconds: float | None) -> None:
    async with get_sessionmaker()() as session, session.begin():
        document = await session.get_one(Document, doc_id)
        document.status = "done"
        document.ingest_seconds = ingest_seconds
        await _maybe_notify_embed(session, document.library_id)
    await persist_document_tree(doc_id)


async def mark_document(doc_id: int, status: str, ingest_seconds: float | None) -> None:
    async with get_sessionmaker()() as session, session.begin():
        document = await session.get_one(Document, doc_id)
        document.status = status
        document.ingest_seconds = ingest_seconds
        await _maybe_notify_embed(session, document.library_id)


async def _load_payloads(session: AsyncSession, ids: list[str]) -> dict[str, dict]:
    return {
        "paragraphs": {
            row.content_id: row.text
            for row in await session.scalars(select(Paragraph).where(Paragraph.content_id.in_(ids)))
        },
        "codes": {
            row.content_id: row.text for row in await session.scalars(select(Code).where(Code.content_id.in_(ids)))
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
            row.content_id: row for row in await session.scalars(select(Table).where(Table.content_id.in_(ids)))
        },
    }


def _node_dict(node: ContentNode, payloads: dict[str, dict]) -> dict:
    data: dict = {"type": node.type}
    if node.level is not None:
        data["level"] = node.level
    if node.label is not None:
        data["label"] = node.label
    if node.type == "level":
        return data
    match detail_kind(node.type):
        case "code":
            data["content"] = payloads["codes"].get(node.content_id)
        case "equation":
            data["content"] = payloads["equations"].get(node.content_id)
        case "list":
            data["list_items"] = payloads["lists"].get(node.content_id)
        case "table":
            table = payloads["tables"].get(node.content_id)
            if table is not None:
                data["columns"] = table.columns
                data["sample_rows"] = table.sample_rows
                data["n_rows"] = table.n_rows
                data["description"] = table.description
                data["metadata"] = table.table_metadata
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


def _tree_key(doc_id: int) -> str:
    return f"{doc_id}.json.zst"


def compress_tree(tree: dict) -> bytes:
    return zstandard.ZstdCompressor().compress(json.dumps(tree).encode())


async def persist_document_tree(doc_id: int) -> None:
    async with get_sessionmaker()() as session:
        document = await session.get_one(Document, doc_id)
        tree = await build_document_tree(session, document)
    payload = await asyncio.to_thread(compress_tree, tree)
    await asyncio.to_thread(put_object, _tree_key(doc_id), payload)


def delete_document_tree(doc_id: int) -> None:
    delete_object(_tree_key(doc_id))


def load_document_tree(doc_id: int) -> dict | None:
    raw = get_object(_tree_key(doc_id))
    if raw is None:
        return None
    return json.loads(zstandard.ZstdDecompressor().decompress(raw))


async def get_result(doc_id: int) -> dict:
    tree = await asyncio.to_thread(load_document_tree, doc_id)
    if tree is None:
        raise HTTPException(status_code=404, detail="result not ready")
    return tree

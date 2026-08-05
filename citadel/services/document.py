import asyncio
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime

import zstandard
from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import TypeAdapter
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from citadel.db import get_sessionmaker
from citadel.models.content import ContentNode
from citadel.models.document import Document
from citadel.models.library import Library
from citadel.models.status import DocumentStatus, LibraryStatus
from citadel.models.table import Table, TableRow
from citadel.schemas.content import Block
from citadel.schemas.tree import ContentBlock
from citadel.services.excel import SheetItem, SheetText
from citadel.services.grid import classify_grid
from citadel.services.tabular import (
    grid_from_html,
    html_to_text,
    stitch_tables,
)
from citadel.services.tree import (
    EMPTY_IMAGE_TYPES,
    build_raw,
    build_search_text,
    build_table_search_text,
    build_tree,
    collapse_loops,
    detail_kind,
    split_paratext,
    split_title,
)
from citadel.storage import delete_object, get_object, put_object
from citadel.tabular.materialize import MaterializedTable
from config import EMBED_MAX_TOKENS, get_embed_tokenizer


async def create_documents(library_id: int, filenames: list[str]) -> list[int]:
    async with get_sessionmaker()() as session, session.begin():
        documents = [Document(library_id=library_id, filename=name, status=DocumentStatus.QUEUED) for name in filenames]
        session.add_all(documents)
        await session.flush()
        return [document.id for document in documents]


async def notify_embed(library_id: int) -> None:
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(text("SELECT pg_notify('embed', :library)"), {"library": str(library_id)})


_LIBRARY_LOCK_CLASS = 1


async def library_inflight(session: AsyncSession, library_id: int) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(Document)
        .where(
            Document.library_id == library_id,
            Document.status.in_((DocumentStatus.QUEUED, DocumentStatus.PROCESSING)),
        )
    )
    return count or 0


async def _maybe_notify_embed(session: AsyncSession, library_id: int) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:cls, :library)"), {"cls": _LIBRARY_LOCK_CLASS, "library": library_id}
    )
    library = await session.get(Library, library_id)
    if library is None:
        return
    if await library_inflight(session, library_id) != 0:
        return
    now = datetime.now(UTC)
    if library.ingested_at is None:
        library.ingested_at = now
    library.status = LibraryStatus.INGESTED
    if library.tier == "tier_2":
        await session.execute(text("SELECT pg_notify('embed', :library)"), {"library": str(library_id)})


async def begin_library_ingest(library_id: int) -> None:
    async with get_sessionmaker()() as session, session.begin():
        library = await session.get(Library, library_id)
        if library is None or library.status == LibraryStatus.PROCESSING:
            return
        library.status = LibraryStatus.PROCESSING
        library.ingest_started_at = datetime.now(UTC)
        library.ingested_at = None
        library.finalize_started_at = None
        library.described_at = None
        library.embed_started_at = None
        library.ready_at = None


async def mark_processing(doc_id: int) -> None:
    async with get_sessionmaker()() as session, session.begin():
        document = await session.get(Document, doc_id)
        if document is None or document.status != DocumentStatus.QUEUED:
            return
        document.status = DocumentStatus.PROCESSING
        document.processing_started_at = datetime.now(UTC)


def _ingest_seconds(document: Document) -> float | None:
    if document.processing_started_at is None:
        return None
    return (datetime.now(UTC) - document.processing_started_at).total_seconds()


async def mark_finalize_started(library_id: int) -> None:
    async with get_sessionmaker()() as session, session.begin():
        library = await session.get(Library, library_id)
        if library is None:
            return
        library.finalize_started_at = datetime.now(UTC)


async def mark_described(library_id: int) -> None:
    async with get_sessionmaker()() as session, session.begin():
        library = await session.get(Library, library_id)
        if library is None:
            return
        library.described_at = datetime.now(UTC)


async def mark_embed_started(library_id: int) -> None:
    async with get_sessionmaker()() as session, session.begin():
        library = await session.get(Library, library_id)
        if library is None:
            return
        library.embed_started_at = datetime.now(UTC)


async def mark_library_ready(library_id: int) -> None:
    async with get_sessionmaker()() as session, session.begin():
        library = await session.get(Library, library_id)
        if library is None:
            return
        library.status = LibraryStatus.READY
        library.ready_at = datetime.now(UTC)


def _reclassify_regions(blocks: list[Block]) -> list[Block]:
    kept: list[Block] = []
    for block in blocks:
        if block.type != "table":
            kept.append(block)
            continue
        grid, _ = grid_from_html(block.text or "")
        text = " ".join(cell for row in grid for cell in row if cell.strip())
        match classify_grid(grid):
            case "empty":
                salvaged = html_to_text(block.text or "")
                if salvaged:
                    kept.append(block.model_copy(update={"type": "text", "text": salvaged}))
            case "equation":
                kept.append(block.model_copy(update={"type": "equation", "text": text}))
            case "prose":
                kept.append(block.model_copy(update={"type": "text", "text": text}))
            case _:
                kept.append(block)
    return kept


def table_block_indices(blocks: list[Block]) -> list[int]:
    return [index for index, block in enumerate(blocks) if block.type == "table"]


@dataclass
class _Prepared:
    stitched: list[Block]
    paratext: list[str]
    drops: dict[str, int]
    title: str | None = None


def prepare_document(blocks: list[Block]) -> _Prepared:
    return _prepare_blocks(blocks)


_TABLES_ADAPTER = TypeAdapter(list[MaterializedTable])


def dump_structures(prepared: _Prepared) -> str:
    return json.dumps(
        {
            "stitched": [block.model_dump() for block in prepared.stitched],
            "paratext": prepared.paratext,
            "drops": prepared.drops,
        }
    )


def load_structures(raw: str | bytes | None) -> _Prepared:
    if raw is None:
        return _Prepared([], [], {})
    data = json.loads(raw)
    return _Prepared(
        stitched=[Block(**block) for block in data["stitched"]], paratext=data["paratext"], drops=data["drops"]
    )


def dump_tables(tables: list[MaterializedTable]) -> str:
    return json.dumps(_TABLES_ADAPTER.dump_python(tables, mode="json"))


def collect_tables(results: dict[int, str | bytes]) -> tuple[dict[int, int], list[MaterializedTable]]:
    counts: dict[int, int] = {}
    queue: list[MaterializedTable] = []
    for index in sorted(results):
        tables = _TABLES_ADAPTER.validate_python(json.loads(results[index]))
        counts[index] = len(tables)
        queue.extend(tables)
    return counts, queue


def _token_count(text: str | None) -> int:
    if not text:
        return 0
    return min(EMBED_MAX_TOKENS, len(get_embed_tokenizer()(text, add_special_tokens=True)["input_ids"]))


def _block_row(block: ContentBlock, doc_id: int, search: str | None) -> dict[str, object]:
    return {
        "document_id": doc_id,
        "ordinal": block.ordinal,
        "page_no": block.page_no,
        "type": block.type,
        "heading": block.heading,
        "bbox": block.bbox,
        "raw": build_raw(block),
        "search_text": search,
        "token_count": _token_count(search),
    }


def _table_search(table: MaterializedTable, library_name: str, filename: str) -> str:
    return build_table_search_text(
        library_name,
        filename,
        "",
        table.title,
        table.caption,
        table.notes,
        [column.header or "" for column in table.columns],
    )


async def _insert_blocks(session: AsyncSession, rows: list[dict[str, object]]) -> list[int]:
    result = await session.execute(insert(ContentNode).returning(ContentNode.id), rows)
    return list(result.scalars())


async def _insert_tables_bulk(session: AsyncSession, doc_id: int, pairs: list[tuple[int, MaterializedTable]]) -> None:
    if not pairs:
        return
    table_payload = [
        {
            "content_id": content_id,
            "document_id": doc_id,
            "columns": [column.model_dump() for column in table.columns],
            "table_metadata": {
                "title": table.title,
                "caption": table.caption,
                "notes": table.notes,
                "formulas": table.formulas,
            },
            "n_rows": table.n_rows,
            "sample_rows": table.sample_rows,
            "anchors": table.anchors,
        }
        for content_id, table in pairs
    ]
    result = await session.execute(insert(Table).returning(Table.id, Table.content_id), table_payload)
    table_ids = {table_content_id: table_id for table_id, table_content_id in result}
    row_payload = [
        {"table_id": table_ids[content_id], "row_idx": index, "values": values}
        for content_id, table in pairs
        for index, values in enumerate(table.rows)
    ]
    if row_payload:
        await session.execute(insert(TableRow), row_payload)


DROP_REASONS = frozenset({"paratext", "empty_image", "empty_block"})


def _drop_counts(blocks: list[Block], content: list[Block], reclassified: list[Block]) -> dict[str, int]:
    empty_image = sum(1 for block in blocks if block.type in EMPTY_IMAGE_TYPES and not (block.text or "").strip())
    counts = {
        "paratext": len(blocks) - len(content) - empty_image,
        "empty_image": empty_image,
        "empty_block": len(content) - len(reclassified),
    }
    unknown = set(counts) - DROP_REASONS
    if unknown:
        msg = f"undeclared drop reason(s) {sorted(unknown)}"
        raise ValueError(msg)
    negative = sorted(reason for reason, count in counts.items() if count < 0)
    if negative:
        msg = f"drop accounting is broken for {negative}"
        raise ValueError(msg)
    return {reason: count for reason, count in counts.items() if count}


_LOOP_EXEMPT_TYPES = {"table"}


def _collapse_block_loops(blocks: list[Block]) -> list[Block]:
    collapsed: list[Block] = []
    for block in blocks:
        if block.type in _LOOP_EXEMPT_TYPES or not block.text:
            collapsed.append(block)
            continue
        text = collapse_loops(block.text)
        collapsed.append(block if text == block.text else block.model_copy(update={"text": text}))
    return collapsed


def _prepare_blocks(blocks: list[Block]) -> _Prepared:
    deduped = _collapse_block_loops(blocks)
    content_blocks, paratext = split_paratext(deduped)
    reclassified = _reclassify_regions(content_blocks)
    drops = _drop_counts(deduped, content_blocks, reclassified)
    title, titleless = split_title(reclassified)
    return _Prepared(stitch_tables(titleless), paratext, drops, title)


async def save_document_tree(
    doc_id: int,
    blocks: list[Block],
    status: DocumentStatus,
    prepared: _Prepared,
    table_counts: dict[int, int],
    table_queue: list[MaterializedTable],
) -> None:
    async with get_sessionmaker()() as session:
        document = await session.get(Document, doc_id)
        if document is None:
            return
        library = await session.get_one(Library, document.library_id)
        library_id, filename, library_name = document.library_id, document.filename, library.name
    await asyncio.to_thread(persist_document_blocks, doc_id, blocks)
    built = list(build_tree(prepared.stitched, table_counts))
    search_text = build_search_text(built, library_name, filename)
    tables = iter(table_queue)
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": doc_id})
        document = await session.get(Document, doc_id)
        if document is None:
            return
        document.status = status
        document.title = prepared.title
        document.ingest_seconds = _ingest_seconds(document)
        document.blocks_in = len(blocks)
        document.nodes_out = len(built)
        document.drops = prepared.drops
        document.paratext = prepared.paratext
        if await session.scalar(select(ContentNode.id).where(ContentNode.document_id == doc_id).limit(1)) is not None:
            await _maybe_notify_embed(session, library_id)
            return
        rows: list[dict[str, object]] = []
        table_blocks: list[tuple[int, MaterializedTable]] = []
        for index, block in enumerate(built):
            if block.kind == "table":
                table = next(tables)
                table_blocks.append((index, table))
                rows.append(_block_row(block, doc_id, _table_search(table, library_name, filename)))
            else:
                rows.append(_block_row(block, doc_id, search_text.get(index)))
        ids = await _insert_blocks(session, rows)
        await _insert_tables_bulk(session, doc_id, [(ids[index], table) for index, table in table_blocks])
        await _maybe_notify_embed(session, library_id)


def _sheet_text_search(library_name: str, filename: str, sheet_name: str, text: str) -> str:
    return build_table_search_text(library_name, filename, sheet_name, None, None, [], [text])


def _add_sheet_text(
    session: AsyncSession, item: SheetText, doc_id: int, sheet_no: int, ordinal: int, search: str
) -> None:
    session.add(
        ContentNode(
            document_id=doc_id,
            sheet_no=sheet_no,
            ordinal=ordinal,
            type="text",
            heading=None,
            raw={"text": item.text},
            search_text=search,
            token_count=_token_count(search),
        )
    )


async def save_sheet_tables(doc_id: int, sheet_no: int, sheet_name: str, items: list[tuple[int, SheetItem]]) -> None:
    async with get_sessionmaker()() as session, session.begin():
        existing = await session.scalar(
            select(ContentNode.id).where(ContentNode.document_id == doc_id, ContentNode.sheet_no == sheet_no).limit(1)
        )
        if existing is not None:
            return
        document = await session.get_one(Document, doc_id)
        library = await session.get_one(Library, document.library_id)
        for ordinal, item in items:
            if isinstance(item, SheetText):
                search = _sheet_text_search(library.name, document.filename, sheet_name, item.text)
                _add_sheet_text(session, item, doc_id, sheet_no, ordinal, search)
                continue
            table = item
            node_search = build_table_search_text(
                library.name,
                document.filename,
                sheet_name,
                table.title,
                table.caption,
                table.notes,
                [column.header or "" for column in table.columns],
            )
            node = ContentNode(
                document_id=doc_id,
                sheet_no=sheet_no,
                ordinal=ordinal,
                type="table",
                heading=None,
                search_text=node_search,
                token_count=_token_count(node_search),
            )
            session.add(node)
            await session.flush()
            row = Table(
                content_id=node.id,
                document_id=doc_id,
                columns=[column.model_dump() for column in table.columns],
                table_metadata={
                    "sheet": sheet_name,
                    "title": table.title,
                    "caption": table.caption,
                    "notes": table.notes,
                    "formulas": table.formulas,
                },
                n_rows=table.n_rows,
                sample_rows=table.sample_rows,
                anchors=table.anchors,
            )
            session.add(row)
            await session.flush()
            session.add_all(
                TableRow(table_id=row.id, row_idx=index, values=values) for index, values in enumerate(table.rows)
            )


async def finalize_tabular(doc_id: int) -> None:
    async with get_sessionmaker()() as session, session.begin():
        document = await session.get(Document, doc_id)
        if document is None:
            return
        document.status = DocumentStatus.INGESTED
        document.ingest_seconds = _ingest_seconds(document)
        await _maybe_notify_embed(session, document.library_id)
    await persist_document_tree(doc_id)


async def mark_document(doc_id: int, status: DocumentStatus) -> None:
    async with get_sessionmaker()() as session, session.begin():
        document = await session.get(Document, doc_id)
        if document is None:
            return
        document.status = status
        document.ingest_seconds = _ingest_seconds(document)
        await _maybe_notify_embed(session, document.library_id)


async def _load_tables(session: AsyncSession, doc_id: int) -> dict[int, Table]:
    return {row.content_id: row for row in await session.scalars(select(Table).where(Table.document_id == doc_id))}


def _block_dict(node: ContentNode, tables: dict[int, Table]) -> dict:
    data: dict = {"type": node.type, "page_no": node.page_no, "sheet_no": node.sheet_no, "heading": node.heading}
    table = tables.get(node.id)
    if table is not None:
        data["columns"] = table.columns
        data["sample_rows"] = table.sample_rows
        data["n_rows"] = table.n_rows
        data["metadata"] = table.table_metadata
    elif node.raw is not None:
        data.update(node.raw)
    return data


async def build_document_tree(session: AsyncSession, document: Document) -> dict:
    nodes = list(
        await session.scalars(
            select(ContentNode).where(ContentNode.document_id == document.id).order_by(ContentNode.block_ordinal)
        )
    )
    tables = await _load_tables(session, document.id)
    blocks = [_block_dict(node, tables) for node in nodes]
    return {"type": "document", "filename": document.filename, "title": document.title, "blocks": blocks}


def _tree_key(doc_id: int) -> str:
    return f"{doc_id}.json.zst"


def _blocks_key(doc_id: int) -> str:
    return f"blocks/{doc_id}.json.zst"


def compress_blocks(blocks: list[Block]) -> bytes:
    payload = [block.model_dump() for block in blocks]
    return zstandard.ZstdCompressor().compress(json.dumps(payload).encode())


def persist_document_blocks(doc_id: int, blocks: list[Block]) -> None:
    put_object(_blocks_key(doc_id), compress_blocks(blocks))


def delete_document_blocks(doc_id: int) -> None:
    delete_object(_blocks_key(doc_id))


def compress_tree(tree: dict) -> bytes:
    return zstandard.ZstdCompressor().compress(json.dumps(tree).encode())


async def persist_document_tree(doc_id: int) -> None:
    async with get_sessionmaker()() as session:
        document = await session.get(Document, doc_id)
        if document is None:
            return
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


def _md_cell(value: object) -> str:
    return "" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def _md_table(columns: list[dict], rows: list[list]) -> list[str]:
    headers = [str(column.get("header") or "") for column in columns]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(_md_cell(value) for value in row) + " |" for row in rows)
    lines.append("")
    return lines


def _md_block(node: ContentNode, tables: dict[int, Table], table_rows: dict[int, list[list]], lines: list[str]) -> None:
    table = tables.get(node.id)
    if table is not None:
        lines.extend(_md_table(table.columns, table_rows.get(table.id, [])))
        return
    raw = node.raw or {}
    match detail_kind(node.type):
        case "list":
            lines.extend(
                "  " * int(item.get("depth", 0) or 0) + "- " + str(item.get("content", ""))
                for item in raw.get("items") or []
            )
            lines.append("")
        case "code":
            lines.extend(["```", raw.get("text") or "", "```", ""])
        case "equation":
            lines.extend(["$$", raw.get("latex") or "", "$$", ""])
        case _:
            if raw.get("text"):
                lines.extend([raw["text"], ""])


async def _document_markdown(session: AsyncSession, doc_id: int) -> str:
    nodes = list(
        await session.scalars(
            select(ContentNode).where(ContentNode.document_id == doc_id).order_by(ContentNode.block_ordinal)
        )
    )
    tables = await _load_tables(session, doc_id)
    table_rows: dict[int, list[list]] = {}
    table_ids = [table.id for table in tables.values()]
    if table_ids:
        rows = await session.scalars(
            select(TableRow).where(TableRow.table_id.in_(table_ids)).order_by(TableRow.row_idx)
        )
        for row in rows:
            table_rows.setdefault(row.table_id, []).append(row.values)
    lines: list[str] = []
    heading: str | None = None
    for node in nodes:
        if node.heading != heading:
            heading = node.heading
            if heading:
                lines.extend(["## " + heading, ""])
        _md_block(node, tables, table_rows, lines)
    return "\n".join(lines).strip() + "\n"


async def export_markdown(library_id: int) -> Response:
    async with get_sessionmaker()() as session:
        library = await session.get(Library, library_id)
        if library is None:
            raise HTTPException(status_code=404, detail="unknown library")
        docs = list(
            await session.execute(
                select(Document.id, Document.filename).where(Document.library_id == library_id).order_by(Document.id)
            )
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for doc_id, filename in docs:
                archive.writestr(f"{filename.rsplit('.', 1)[0]}.md", await _document_markdown(session, doc_id))
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{library.name}.zip"'},
    )

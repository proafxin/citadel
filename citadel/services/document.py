import asyncio
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import starmap

import zstandard
from fastapi import HTTPException
from fastapi.responses import Response
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from citadel.db import get_sessionmaker
from citadel.llm import describe_table
from citadel.models.content import Code, ContentNode, Equation, ListBlock, Paragraph
from citadel.models.document import Document
from citadel.models.library import Library
from citadel.models.status import DocumentStatus, LibraryStatus
from citadel.models.table import Table, TableRow
from citadel.schemas.content import Block
from citadel.schemas.table import Column
from citadel.services.excel import MaterializedTable, SheetItem, SheetText
from citadel.services.grid import classify_grid
from citadel.services.tabular import grid_from_html, html_to_text, stitch_tables, structure_html_tables
from citadel.services.tree import (
    EMPTY_IMAGE_TYPES,
    NodeSpec,
    build_search_text,
    build_table_search_text,
    build_tree,
    collapse_loops,
    detail_kind,
    make_content_id,
    split_paratext,
)
from citadel.storage import delete_object, get_object, put_object
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


_LIBRARY_LOCK_CLASS = 1  # namespace for the per-library advisory lock (two-arg space, disjoint from the doc lock)


async def library_inflight(session: AsyncSession, library_id: int) -> int:
    # documents the library is still working on. embedding must never start while this is nonzero: it would run BGE-M3
    # against a partial library AND contend with the OCR model for the same GPU
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
    # serialize completion of docs in the SAME library: without this, two docs finishing concurrently each see the other
    # still PROCESSING (uncommitted), so neither observes inflight==0 and neither notifies → the library never embeds.
    # the lock holder counts + notifies + commits; the next waiter then sees the prior doc committed. two-arg lock space
    # never collides with save_document_tree's single-arg per-doc lock, and every caller takes it doc-then-library, so
    # there is no lock-order cycle.
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


async def _describe_one(
    content_id: str,
    columns: list[dict],
    sample_rows: list[list],
    metadata: dict,
    filename: str,
    search_text: str | None,
) -> tuple[str, str, str]:
    description = await describe_table(
        [Column(**column) for column in columns],
        sample_rows,
        metadata.get("sheet") or filename,
        metadata.get("formulas"),
    )
    combined = "\n".join(part for part in [search_text or "", description.strip()] if part)
    return content_id, description, combined


async def describe_library_tables(library_id: int) -> int:
    async with get_sessionmaker()() as session:
        rows = list(
            await session.execute(
                select(
                    Table.content_id,
                    Table.columns,
                    Table.sample_rows,
                    Table.table_metadata,
                    Document.filename,
                    ContentNode.search_text,
                )
                .join(Document, Table.document_id == Document.id)
                .join(ContentNode, ContentNode.content_id == Table.content_id)
                .where(Document.library_id == library_id, Table.description == "")
            )
        )
    if not rows:
        return 0
    described = await asyncio.gather(*starmap(_describe_one, rows))
    async with get_sessionmaker()() as session, session.begin():
        for content_id, description, combined in described:
            await session.execute(update(Table).where(Table.content_id == content_id).values(description=description))
            await session.execute(
                update(ContentNode)
                .where(ContentNode.content_id == content_id)
                .values(search_text=combined, token_count=_token_count(combined))
            )
    return len(described)


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


def _reclassify_regions(blocks: list[Block]) -> list[Block]:
    # the visual model labels displayed math, prose and empty regions as "table". re-type each to the leaf it really is
    # (equation / paragraph / dropped) so it never reaches table structuring, where it would become a col0..colN relation
    kept: list[Block] = []
    for block in blocks:
        if block.type != "table":
            kept.append(block)
            continue
        grid = grid_from_html(block.text or "")
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


async def _resolve_tables(blocks: list[Block]) -> tuple[dict[int, int], list[MaterializedTable]]:
    counts: dict[int, int] = {}
    queue: list[MaterializedTable] = []
    for idx, block in enumerate(blocks):
        if block.type == "table":
            tables = await structure_html_tables(block.text or "")
            counts[idx] = len(tables)
            queue.extend(tables)
    return counts, queue


def _token_count(text: str | None) -> int:
    # exact BGE-M3 token count (capped at the model ceiling), computed at ingestion so embedding reads it off the row
    if not text:
        return 0
    return min(EMBED_MAX_TOKENS, len(get_embed_tokenizer()(text, add_special_tokens=True)["input_ids"]))


def _node_row(spec: NodeSpec, doc_id: int, search: str | None) -> dict[str, object]:
    return {
        "content_id": spec.content_id,
        "document_id": doc_id,
        "ordinal": spec.ordinal,
        "page_no": spec.page_no,
        "type": spec.type,
        "level": spec.level,
        "label": spec.text if spec.kind == "heading" else None,
        "bbox": spec.bbox,
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
        table.description,
    )


async def _insert_nodes_bfs(session: AsyncSession, specs: list[NodeSpec], rows: dict[str, dict[str, object]]) -> None:
    # BFS by depth: all nodes at one level go in a single INSERT ... RETURNING (id, content_id); the returned ids feed
    # the next level's parent_id. specs are already parent-before-child, so depth is a one-pass computation and each
    # level's parents are guaranteed present. ~tree-depth statements instead of one flush per node.
    parent_of = {spec.content_id: spec.parent_content_id for spec in specs}
    depth: dict[str, int] = {}
    levels: dict[int, list[str]] = {}
    for spec in specs:
        node_depth = 0 if spec.parent_content_id is None else depth[spec.parent_content_id] + 1
        depth[spec.content_id] = node_depth
        levels.setdefault(node_depth, []).append(spec.content_id)
    id_map: dict[str, int] = {}
    for level in sorted(levels):
        payload = []
        for content_id in levels[level]:
            parent = parent_of[content_id]
            payload.append({**rows[content_id], "parent_id": id_map[parent] if parent is not None else None})
        result = await session.execute(insert(ContentNode).returning(ContentNode.id, ContentNode.content_id), payload)
        id_map.update({node_content_id: node_id for node_id, node_content_id in result})


async def _insert_tables_bulk(session: AsyncSession, doc_id: int, pairs: list[tuple[str, MaterializedTable]]) -> None:
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
            "description": table.description,
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


@dataclass
class _Prepared:
    stitched: list[Block]
    paratext: list[str]
    drops: dict[str, int]


def _drop_counts(blocks: list[Block], content: list[Block], reclassified: list[Block]) -> dict[str, int]:
    # DERIVED, not re-predicated: split_paratext now RESCUES a mis-typed paratext block into content, so re-testing
    # the type here would count a kept block as dropped. whatever it removed is either an empty image or paratext.
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


_LOOP_EXEMPT_TYPES = {"table"}  # a table may legitimately repeat identical rows; only prose/math loop pathologically


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
    deduped = _collapse_block_loops(blocks)  # kill VLM repetition loops before anything downstream sees them
    content_blocks, paratext = split_paratext(deduped)
    reclassified = _reclassify_regions(content_blocks)  # math/prose/empty must not reach table structuring
    drops = _drop_counts(deduped, content_blocks, reclassified)
    return _Prepared(stitch_tables(reclassified), paratext, drops)


async def save_document_tree(doc_id: int, blocks: list[Block], status: DocumentStatus) -> None:
    async with get_sessionmaker()() as session:
        document = await session.get(Document, doc_id)
        if document is None:
            return
        library = await session.get_one(Library, document.library_id)
        library_id, filename, library_name = document.library_id, document.filename, library.name
    await asyncio.to_thread(persist_document_blocks, doc_id, blocks)
    prepared = _prepare_blocks(blocks)
    table_counts, table_queue = await _resolve_tables(prepared.stitched)
    specs = list(build_tree(prepared.stitched, library_id, doc_id, table_counts))
    search_text = build_search_text(specs, library_name, filename)
    tables = iter(table_queue)
    async with get_sessionmaker()() as session, session.begin():
        # per-doc advisory lock: serialize concurrent/redelivered merges of the same document so the "already
        # written?" check and the insert are one atomic unit. the lock holder writes the tree; any other caller waits,
        # then finds it present and only refreshes status — never a duplicate content_id, no TOCTOU
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": doc_id})
        document = await session.get(Document, doc_id)
        if document is None:
            return
        document.status = status
        document.ingest_seconds = _ingest_seconds(document)
        document.blocks_in = len(blocks)
        document.nodes_out = len(specs)
        document.drops = prepared.drops
        document.paratext = prepared.paratext
        if await session.scalar(select(ContentNode.id).where(ContentNode.document_id == doc_id).limit(1)) is not None:
            await _maybe_notify_embed(session, library_id)
            return
        node_rows: dict[str, dict[str, object]] = {}
        table_pairs: list[tuple[str, MaterializedTable]] = []
        detail_specs: list[NodeSpec] = []
        for spec in specs:
            if spec.kind == "table":
                table = next(tables)
                table_pairs.append((spec.content_id, table))
                node_rows[spec.content_id] = _node_row(spec, doc_id, _table_search(table, library_name, filename))
            else:
                node_rows[spec.content_id] = _node_row(spec, doc_id, search_text.get(spec.content_id))
                if spec.kind != "heading":
                    detail_specs.append(spec)
        await _insert_nodes_bfs(session, specs, node_rows)
        for spec in detail_specs:
            _add_detail(session, spec, doc_id)
        await _insert_tables_bulk(session, doc_id, table_pairs)
        await _maybe_notify_embed(session, library_id)


def _add_sheet_text(
    session: AsyncSession, item: SheetText, content_id: str, doc_id: int, parent_id: int, sheet_no: int, ordinal: int
) -> None:
    session.add(
        ContentNode(
            content_id=content_id,
            document_id=doc_id,
            parent_id=parent_id,
            sheet_no=sheet_no,
            ordinal=ordinal,
            type="text",
            search_text=item.text,
            token_count=_token_count(item.text),
        )
    )
    session.add(Paragraph(content_id=content_id, text=item.text))


async def save_sheet_tables(doc_id: int, sheet_no: int, sheet_name: str, items: list[tuple[int, SheetItem]]) -> None:
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
        for ordinal, item in items:
            content_id = make_content_id(document.library_id, doc_id, sheet_no, ordinal)
            if isinstance(item, SheetText):
                _add_sheet_text(session, item, content_id, doc_id, sheet.id, sheet_no, ordinal)
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
                token_count=_token_count(node_search),
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
                    "formulas": table.formulas,
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


def _blocks_key(doc_id: int) -> str:
    return f"blocks/{doc_id}.json.zst"


def compress_blocks(blocks: list[Block]) -> bytes:
    payload = [block.model_dump() for block in blocks]
    return zstandard.ZstdCompressor().compress(json.dumps(payload).encode())


def persist_document_blocks(doc_id: int, blocks: list[Block]) -> None:
    put_object(_blocks_key(doc_id), compress_blocks(blocks))


def load_document_blocks(doc_id: int) -> list[Block] | None:
    raw = get_object(_blocks_key(doc_id))
    if raw is None:
        return None
    payload = json.loads(zstandard.ZstdDecompressor().decompress(raw))
    return [Block.model_validate(item) for item in payload]


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


def _md_node(
    node: ContentNode,
    children_of: dict[int | None, list[ContentNode]],
    payloads: dict[str, dict],
    tables: dict[str, Table],
    table_rows: dict[int, list[list]],
    lines: list[str],
) -> None:
    if node.type == "level":
        lines.extend(["#" * min(node.level or 1, 6) + " " + (node.label or ""), ""])
    else:
        match detail_kind(node.type):
            case "table":
                table = tables.get(node.content_id)
                if table is not None:
                    lines.extend(_md_table(table.columns, table_rows.get(table.id, [])))
            case "list":
                lines.extend(
                    "  " * int(item.get("depth", 0) or 0) + "- " + str(item.get("content", ""))
                    for item in payloads["lists"].get(node.content_id) or []
                )
                lines.append("")
            case "code":
                lines.extend(["```", payloads["codes"].get(node.content_id) or "", "```", ""])
            case "equation":
                lines.extend(["$$", payloads["equations"].get(node.content_id) or "", "$$", ""])
            case _:
                text = payloads["paragraphs"].get(node.content_id)
                if text:
                    lines.extend([text, ""])
    for child in children_of.get(node.id, []):
        _md_node(child, children_of, payloads, tables, table_rows, lines)


async def _document_markdown(session: AsyncSession, doc_id: int) -> str:
    nodes = list(
        await session.scalars(
            select(ContentNode)
            .where(ContentNode.document_id == doc_id)
            .order_by(ContentNode.page_no, ContentNode.ordinal)
        )
    )
    payloads = await _load_payloads(session, [node.content_id for node in nodes])
    tables = {
        table.content_id: table for table in await session.scalars(select(Table).where(Table.document_id == doc_id))
    }
    table_rows: dict[int, list[list]] = {}
    table_ids = [table.id for table in tables.values()]
    if table_ids:
        rows = await session.scalars(
            select(TableRow).where(TableRow.table_id.in_(table_ids)).order_by(TableRow.row_idx)
        )
        for row in rows:
            table_rows.setdefault(row.table_id, []).append(row.values)
    children_of: dict[int | None, list[ContentNode]] = {}
    for node in nodes:
        children_of.setdefault(node.parent_id, []).append(node)
    lines: list[str] = []
    for root in children_of.get(None, []):
        _md_node(root, children_of, payloads, tables, table_rows, lines)
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

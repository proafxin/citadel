import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import bindparam, delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from citadel.db import get_sessionmaker
from citadel.llm import call_slm, count_tokens, count_tokens_batch
from citadel.models.batch import ContentBatch
from citadel.models.content import ContentNode
from citadel.models.document import Document
from citadel.models.table import Table
from citadel.prompts import load_prompt

logger = logging.getLogger(__name__)

BATCH_TOKENS = 32768
SAMPLE_ROWS = 3
SUMMARY_RATIO = 0.1
SUMMARY_TOKENS_MAX = 4096

_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}

_BLOCK_ORDINALS = text("""
UPDATE content SET block_ordinal = seq.rn
FROM (
    SELECT id, row_number() OVER (
        PARTITION BY document_id ORDER BY COALESCE(page_no, sheet_no), ordinal
    ) AS rn
    FROM content WHERE document_id = :doc_id
) AS seq
WHERE content.id = seq.id
""")


@dataclass
class BlockText:
    content_id: int
    block_ordinal: int
    page_no: int | None
    text: str
    tokens: int


@dataclass
class BatchSpec:
    document_id: int
    batch_no: int
    start_block_ordinal: int
    end_block_ordinal: int
    start_page_no: int | None
    end_page_no: int | None
    text: str
    content_tokens: int


async def assign_block_ordinals(session: AsyncSession, doc_id: int) -> None:
    await session.execute(_BLOCK_ORDINALS, {"doc_id": doc_id})


def _table_text(table: Table) -> str:
    headers = [str(column.get("header") or "") for column in table.columns]
    lines = [" | ".join(headers)]
    lines.extend(
        " | ".join("" if value is None else str(value) for value in row) for row in table.sample_rows[:SAMPLE_ROWS]
    )
    return "\n".join(lines)


def render_raw(raw: dict | None) -> str:
    # the block's structure is part of its meaning: an item's position answers "the second item", indentation carries
    # nesting, LaTeX needs its delimiters. search_text flattens all of that away, so it is for matching only — anything
    # a model has to READ (a summary's input, an answer's evidence) is rendered from raw
    fields = raw or {}
    if items := fields.get("items"):
        return "\n".join(
            "  " * int(item.get("depth", 0) or 0) + f"{int(item.get('ordinal', 0)) + 1}. {item.get('content', '')}"
            for item in items
        )
    if latex := fields.get("latex"):
        return f"$$\n{latex}\n$$"
    return str(fields.get("text") or "")


def render_block(node: ContentNode, table: Table | None) -> str:
    return _table_text(table) if table is not None else render_raw(node.raw)


async def load_blocks(session: AsyncSession, doc_id: int) -> list[BlockText]:
    nodes = list(
        await session.scalars(
            select(ContentNode).where(ContentNode.document_id == doc_id).order_by(ContentNode.block_ordinal)
        )
    )
    tables = {row.content_id: row for row in await session.scalars(select(Table).where(Table.document_id == doc_id))}
    kept = [(node, body) for node in nodes if (body := render_block(node, tables.get(node.id))).strip()]
    counts = count_tokens_batch([body for _, body in kept])
    return [
        BlockText(
            content_id=node.id,
            block_ordinal=node.block_ordinal or 0,
            page_no=node.page_no,
            text=body,
            tokens=tokens,
        )
        for (node, body), tokens in zip(kept, counts, strict=True)
    ]


def _spec(document_id: int, batch_no: int, blocks: list[BlockText]) -> BatchSpec:
    pages = [block.page_no for block in blocks if block.page_no is not None]
    return BatchSpec(
        document_id=document_id,
        batch_no=batch_no,
        start_block_ordinal=blocks[0].block_ordinal,
        end_block_ordinal=blocks[-1].block_ordinal,
        start_page_no=min(pages) if pages else None,
        end_page_no=max(pages) if pages else None,
        text="\n\n".join(block.text for block in blocks),
        content_tokens=sum(block.tokens for block in blocks),
    )


def pack_batches(document_id: int, blocks: list[BlockText]) -> list[BatchSpec]:
    specs: list[BatchSpec] = []
    current: list[BlockText] = []
    size = 0
    for block in blocks:
        if current and size + block.tokens > BATCH_TOKENS:
            specs.append(_spec(document_id, len(specs) + 1, current))
            current, size = [], 0
        current.append(block)
        size += block.tokens
    if current:
        specs.append(_spec(document_id, len(specs) + 1, current))
    return specs


def summary_budget(content_tokens: int) -> int:
    return min(int(content_tokens * SUMMARY_RATIO), SUMMARY_TOKENS_MAX)


async def summarize(spec: BatchSpec) -> str:
    instructions = load_prompt("batch_summary").replace("{summary_tokens}", str(summary_budget(spec.content_tokens)))
    data = await call_slm(f"{instructions}\ntext:\n{spec.text}", _SUMMARY_SCHEMA, interactive=False)
    return str(data.get("summary", "")).strip()


async def store_token_counts(session: AsyncSession, blocks: list[BlockText]) -> None:
    if not blocks:
        return
    await session.execute(
        update(ContentNode).where(ContentNode.id == bindparam("cid")).values(qwen_token_count=bindparam("count")),
        [{"cid": block.content_id, "count": block.tokens} for block in blocks],
    )


async def build_document_specs(session: AsyncSession, doc_id: int) -> list[BatchSpec]:
    await assign_block_ordinals(session, doc_id)
    blocks = await load_blocks(session, doc_id)
    if not blocks:
        return []
    await store_token_counts(session, blocks)
    return pack_batches(doc_id, blocks)


async def build_library_batches(library_id: int) -> int:
    async with get_sessionmaker()() as session, session.begin():
        doc_ids = list(await session.scalars(select(Document.id).where(Document.library_id == library_id)))
        specs: list[BatchSpec] = []
        for doc_id in doc_ids:
            specs.extend(await build_document_specs(session, doc_id))
    if not specs:
        return 0
    summaries = await asyncio.gather(*(summarize(spec) for spec in specs))
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(delete(ContentBatch).where(ContentBatch.document_id.in_(doc_ids)))
        session.add_all(
            ContentBatch(
                document_id=spec.document_id,
                batch_no=spec.batch_no,
                start_block_ordinal=spec.start_block_ordinal,
                end_block_ordinal=spec.end_block_ordinal,
                start_page_no=spec.start_page_no,
                end_page_no=spec.end_page_no,
                summary=summary,
                summary_tokens=count_tokens(summary),
                content_tokens=spec.content_tokens,
            )
            for spec, summary in zip(specs, summaries, strict=True)
        )
    logger.info("batches library=%d docs=%d batches=%d", library_id, len(doc_ids), len(specs))
    return len(specs)

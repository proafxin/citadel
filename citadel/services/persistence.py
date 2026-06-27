from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from citadel.db import get_sessionmaker
from citadel.models.block import Block as BlockModel
from citadel.models.document import Document
from citadel.models.library import Library
from citadel.schemas.content import Block


async def get_or_create_library(session: AsyncSession, name: str) -> Library:
    existing = await session.scalar(select(Library).where(Library.name == name))
    if existing is not None:
        return existing
    library = Library(name=name)
    session.add(library)
    await session.flush()
    return library


async def create_document(library: str, filename: str) -> int:
    async with get_sessionmaker()() as session, session.begin():
        lib = await get_or_create_library(session, library)
        document = Document(library_id=lib.id, filename=filename, status="pending")
        session.add(document)
        await session.flush()
        return document.id


async def save_document_result(doc_id: int, blocks: list[Block], status: str) -> None:
    async with get_sessionmaker()() as session, session.begin():
        document = await session.get_one(Document, doc_id)
        document.status = status
        session.add_all(
            BlockModel(
                document_id=doc_id,
                page_idx=block.page_idx,
                type=block.type,
                text=block.text,
                text_level=block.text_level,
                list_items=block.list_items,
                bbox=block.bbox,
            )
            for block in blocks
        )


async def get_document_blocks(doc_id: int) -> dict | None:
    async with get_sessionmaker()() as session:
        document = await session.get(Document, doc_id)
        if document is None:
            return None
        rows = await session.scalars(select(BlockModel).where(BlockModel.document_id == doc_id).order_by(BlockModel.id))
        blocks = [
            {
                "page_idx": row.page_idx,
                "type": row.type,
                "text": row.text,
                "text_level": row.text_level,
                "list_items": row.list_items,
                "bbox": row.bbox,
            }
            for row in rows
        ]
        return {"filename": document.filename, "status": document.status, "blocks": blocks}

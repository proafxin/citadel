import asyncio

from fastapi import HTTPException, Response
from sqlalchemy import select

from citadel.db import get_sessionmaker
from citadel.models.document import Document
from citadel.models.library import Library
from citadel.models.status import LibraryStatus
from citadel.schemas.library import LibraryRead, Tier
from citadel.services.document import compress_tree, delete_document_tree, load_document_tree, notify_embed


def _to_read(library: Library) -> LibraryRead:
    return LibraryRead(
        id=library.id,
        name=library.name,
        tier=library.tier,
        status=library.status,
        ingest_started_at=library.ingest_started_at,
        ingested_at=library.ingested_at,
        ready_at=library.ready_at,
    )


async def create_library(name: str, tier: Tier) -> LibraryRead:
    async with get_sessionmaker()() as session, session.begin():
        library = Library(name=name, tier=tier)
        session.add(library)
        await session.flush()
        return _to_read(library)


async def list_libraries() -> list[LibraryRead]:
    async with get_sessionmaker()() as session:
        rows = await session.scalars(select(Library).order_by(Library.id))
        return [_to_read(row) for row in rows]


async def get_library(library_id: int) -> LibraryRead:
    async with get_sessionmaker()() as session:
        library = await session.get(Library, library_id)
        if library is None:
            raise HTTPException(status_code=404, detail="unknown library")
        return _to_read(library)


async def update_library(library_id: int, name: str, tier: Tier) -> LibraryRead:
    async with get_sessionmaker()() as session, session.begin():
        library = await session.get(Library, library_id)
        if library is None:
            raise HTTPException(status_code=404, detail="unknown library")
        upgraded = library.tier != "tier_2" and tier == "tier_2"
        library.name = name
        library.tier = tier
        if upgraded and library.ready_at is not None:
            library.status = LibraryStatus.INGESTED
            library.ready_at = None
        result = _to_read(library)
    if upgraded:
        await notify_embed(library_id)
    return result


async def delete_library(library_id: int) -> None:
    async with get_sessionmaker()() as session, session.begin():
        library = await session.get(Library, library_id)
        if library is None:
            raise HTTPException(status_code=404, detail="unknown library")
        doc_ids = list(await session.scalars(select(Document.id).where(Document.library_id == library_id)))
        await session.delete(library)
    for doc_id in doc_ids:
        await asyncio.to_thread(delete_document_tree, doc_id)


async def library_exists(library_id: int) -> bool:
    async with get_sessionmaker()() as session:
        return await session.get(Library, library_id) is not None


async def build_library_tree(library_id: int) -> dict | None:
    async with get_sessionmaker()() as session:
        library = await session.get(Library, library_id)
        if library is None:
            return None
        name = library.name
        doc_ids = list(
            await session.scalars(select(Document.id).where(Document.library_id == library_id).order_by(Document.id))
        )
    children: list[dict] = []
    for doc_id in doc_ids:
        tree = await asyncio.to_thread(load_document_tree, doc_id)
        if tree is not None:
            children.append(tree)
    return {"type": "library", "name": name, "children": children}


async def download_library(library_id: int) -> Response:
    tree = await build_library_tree(library_id)
    if tree is None:
        raise HTTPException(status_code=404, detail="unknown library")
    payload = await asyncio.to_thread(compress_tree, tree)
    return Response(
        content=payload,
        media_type="application/zstd",
        headers={"Content-Disposition": f'attachment; filename="library_{library_id}.json.zst"'},
    )

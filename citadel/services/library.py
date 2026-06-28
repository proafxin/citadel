import json

import zstandard
from fastapi import HTTPException, Response
from sqlalchemy import select

from citadel.db import get_sessionmaker
from citadel.models.document import Document
from citadel.models.library import Library
from citadel.schemas.library import LibraryRead
from citadel.services.document import load_document_tree


async def create_library(name: str) -> LibraryRead:
    async with get_sessionmaker()() as session, session.begin():
        library = Library(name=name)
        session.add(library)
        await session.flush()
        return LibraryRead(id=library.id, name=library.name)


async def list_libraries() -> list[LibraryRead]:
    async with get_sessionmaker()() as session:
        rows = await session.scalars(select(Library).order_by(Library.id))
        return [LibraryRead(id=row.id, name=row.name) for row in rows]


async def get_library(library_id: int) -> LibraryRead:
    async with get_sessionmaker()() as session:
        library = await session.get(Library, library_id)
        if library is None:
            raise HTTPException(status_code=404, detail="unknown library")
        return LibraryRead(id=library.id, name=library.name)


async def update_library(library_id: int, name: str) -> LibraryRead:
    async with get_sessionmaker()() as session, session.begin():
        library = await session.get(Library, library_id)
        if library is None:
            raise HTTPException(status_code=404, detail="unknown library")
        library.name = name
        return LibraryRead(id=library.id, name=library.name)


async def delete_library(library_id: int) -> None:
    async with get_sessionmaker()() as session, session.begin():
        library = await session.get(Library, library_id)
        if library is None:
            raise HTTPException(status_code=404, detail="unknown library")
        await session.delete(library)


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
    children = [tree for doc_id in doc_ids if (tree := load_document_tree(doc_id)) is not None]
    return {"type": "library", "name": name, "children": children}


async def download_library(library_id: int) -> Response:
    tree = await build_library_tree(library_id)
    if tree is None:
        raise HTTPException(status_code=404, detail="unknown library")
    payload = zstandard.ZstdCompressor().compress(json.dumps(tree).encode())
    return Response(
        content=payload,
        media_type="application/zstd",
        headers={"Content-Disposition": f'attachment; filename="library_{library_id}.json.zst"'},
    )

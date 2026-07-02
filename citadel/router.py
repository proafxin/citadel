from typing import Annotated

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import Response, StreamingResponse

from citadel.schemas.document import DocumentStatus, IngestResponse
from citadel.schemas.library import LibraryCreate, LibraryRead
from citadel.schemas.query import QueryRequest
from citadel.services.document import get_result
from citadel.services.ingestion import get_status, submit_documents
from citadel.services.library import (
    create_library,
    delete_library,
    download_library,
    get_library,
    list_libraries,
    update_library,
)
from citadel.services.query import answer

router = APIRouter()


@router.post("/libraries")
async def post_library(body: LibraryCreate) -> LibraryRead:
    return await create_library(body.name)


@router.get("/libraries")
async def get_libraries() -> list[LibraryRead]:
    return await list_libraries()


@router.get("/libraries/{library_id}")
async def get_one_library(library_id: int) -> LibraryRead:
    return await get_library(library_id)


@router.put("/libraries/{library_id}")
async def put_library(library_id: int, body: LibraryCreate) -> LibraryRead:
    return await update_library(library_id, body.name)


@router.delete("/libraries/{library_id}")
async def remove_library(library_id: int) -> None:
    await delete_library(library_id)


@router.post("/libraries/{library_id}/documents")
async def post_documents(
    library_id: int,
    files: Annotated[list[UploadFile], File(description="Select multiple files to upload")],
) -> IngestResponse:
    return await submit_documents(files, library_id)


@router.get("/libraries/{library_id}/tree")
async def get_library_tree(library_id: int) -> Response:
    return await download_library(library_id)


@router.get("/status/{doc_id}")
async def get_doc_status(doc_id: int) -> DocumentStatus:
    return await get_status(doc_id)


@router.get("/result/{doc_id}")
async def get_doc_result(doc_id: int) -> dict:
    return await get_result(doc_id)


@router.post("/query")
async def post_query(body: QueryRequest) -> StreamingResponse:
    return StreamingResponse(answer(body.question), media_type="text/markdown")

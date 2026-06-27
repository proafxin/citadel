from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse, StreamingResponse

from citadel.services import ingestion

router = APIRouter()


# /search disabled for now — retrieval is off until the HF cache perms are fixed:
#   import asyncio; from citadel.services import retrieval
#   @router.get("/search")
#   async def search(q: str) -> list[dict]:
#       return await asyncio.to_thread(retrieval.search, q)


@router.post("/ingest")
async def ingest(
    files: Annotated[list[UploadFile], File(description="Select multiple files to upload")],
    library: Annotated[str, Form()] = "default",
) -> dict[str, list[str]]:
    doc_ids = [await ingestion.submit_document(await f.read(), f.filename or "upload", library) for f in files]
    return {"doc_ids": doc_ids}


@router.get("/status/{doc_id}")
async def status(doc_id: str) -> dict[str, str]:
    state = await ingestion.get_status(doc_id)
    if not state:
        raise HTTPException(status_code=404, detail="unknown doc_id")
    return state


@router.get("/result/{doc_id}")
async def result(doc_id: str) -> dict:
    payload = await ingestion.get_result(doc_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="result not ready")
    return payload


@router.get("/library/{library_id}")
async def library(library_id: int) -> StreamingResponse:
    if not await ingestion.library_exists(library_id):
        raise HTTPException(status_code=404, detail="unknown library")
    return StreamingResponse(
        ingestion.stream_library_zstd(library_id),
        media_type="application/zstd",
        headers={"Content-Disposition": f'attachment; filename="library_{library_id}.ndjson.zst"'},
    )


@router.get("/markdown/{doc_id}")
async def markdown(doc_id: str) -> PlainTextResponse:
    text = await ingestion.get_markdown(doc_id)
    if text is None:
        raise HTTPException(status_code=404, detail="result not ready")
    return PlainTextResponse(text, media_type="text/markdown")

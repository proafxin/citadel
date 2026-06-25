from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile

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
) -> dict[str, list[str]]:
    doc_ids = [await ingestion.submit_document(await f.read(), f.filename or "upload") for f in files]
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

from typing import Annotated

from fastapi import APIRouter, File, UploadFile

from citadel.schemas.document import DocumentText
from citadel.services.ingestion import transcribe_pdf_bytes

router = APIRouter()


@router.post("/ingest")
async def ingest(file: Annotated[UploadFile, File()]) -> DocumentText:
    data = await file.read()
    return await transcribe_pdf_bytes(data, source=file.filename)

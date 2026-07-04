from pydantic import BaseModel


class IngestResponse(BaseModel):
    doc_ids: list[int]


class DocumentRead(BaseModel):
    id: int
    filename: str
    status: str
    elapsed: float | None = None

from pydantic import BaseModel


class IngestResponse(BaseModel):
    doc_ids: list[int]


class DocumentStatus(BaseModel):
    state: str
    filename: str | None = None
    page_count: int | None = None
    done_count: int | None = None


class DocumentRead(BaseModel):
    id: int
    filename: str
    status: str
    state: str | None = None
    page_count: int | None = None
    done_count: int | None = None

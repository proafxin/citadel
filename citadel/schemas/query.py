from pydantic import BaseModel


class QueryRequest(BaseModel):
    question: str
    library_id: int


class QueryPlan(BaseModel):
    queries: list[str] = []

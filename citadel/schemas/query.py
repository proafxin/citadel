from pydantic import BaseModel


class QueryRequest(BaseModel):
    question: str
    library_id: int


class QueryPlan(BaseModel):
    # only the queries. relevance is not the writer's to state any more — a table reaches it only when the question was
    # already resolved as needing values from that table's rows, and a table that matters for what it IS never arrives
    queries: list[str] = []

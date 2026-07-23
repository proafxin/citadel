from pydantic import BaseModel


class QueryRequest(BaseModel):
    question: str
    library_id: int


class QueryPlan(BaseModel):
    # `tables` is relevance stated OUTRIGHT, separately from `queries`, because SQL cannot express it. a table can
    # matter to a question while needing no query at all — when what is asked about is the table itself (which file,
    # which sheet, how many rows) rather than anything inside its rows. deriving relevance from the SQL alone loses
    # exactly those, and loses any table whose query was rejected or failed
    queries: list[str] = []
    tables: list[int] = []

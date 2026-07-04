from pydantic import BaseModel


class ConfigRead(BaseModel):
    ingest_concurrency: int

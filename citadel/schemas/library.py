from datetime import datetime
from typing import Literal

from pydantic import BaseModel, computed_field

Tier = Literal["tier_1", "tier_2"]


class LibraryCreate(BaseModel):
    name: str
    tier: Tier = "tier_1"


class LibraryRead(BaseModel):
    id: int
    name: str
    tier: str
    status: str
    ingest_started_at: datetime | None = None
    ingested_at: datetime | None = None
    ready_at: datetime | None = None

    @computed_field
    @property
    def ingest_seconds(self) -> float | None:
        if self.ingest_started_at is None or self.ingested_at is None:
            return None
        return (self.ingested_at - self.ingest_started_at).total_seconds()

    @computed_field
    @property
    def finalize_seconds(self) -> float | None:
        if self.ingested_at is None or self.ready_at is None:
            return None
        return (self.ready_at - self.ingested_at).total_seconds()

    @computed_field
    @property
    def total_seconds(self) -> float | None:
        if self.ingest_started_at is None or self.ready_at is None:
            return None
        return (self.ready_at - self.ingest_started_at).total_seconds()

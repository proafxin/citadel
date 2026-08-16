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
    ingest_elapsed_seconds: float = 0.0
    finalize_started_at: datetime | None = None
    described_at: datetime | None = None
    embed_started_at: datetime | None = None
    ready_at: datetime | None = None

    @computed_field
    @property
    def ingest_seconds(self) -> float | None:
        if self.ingest_started_at is None or self.ingested_at is None:
            return None
        return self.ingest_elapsed_seconds + (self.ingested_at - self.ingest_started_at).total_seconds()

    @computed_field
    @property
    def describe_seconds(self) -> float | None:
        if self.finalize_started_at is None or self.described_at is None:
            return None
        return (self.described_at - self.finalize_started_at).total_seconds()

    @computed_field
    @property
    def embed_seconds(self) -> float | None:
        if self.embed_started_at is None or self.ready_at is None:
            return None
        return (self.ready_at - self.embed_started_at).total_seconds()

    @computed_field
    @property
    def finalize_seconds(self) -> float | None:
        if self.finalize_started_at is None or self.ready_at is None:
            return None
        return (self.ready_at - self.finalize_started_at).total_seconds()

    @computed_field
    @property
    def total_seconds(self) -> float | None:
        if self.ingest_seconds is None or self.finalize_seconds is None:
            return None
        return self.ingest_seconds + self.finalize_seconds

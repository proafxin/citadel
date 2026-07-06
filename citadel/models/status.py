from enum import StrEnum


class DocumentStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    INGESTED = "ingested"
    PARTIAL = "partial"
    EMBEDDED = "embedded"
    FAILED = "failed"
    SKIPPED = "skipped"


class LibraryStatus(StrEnum):
    PROCESSING = "processing"
    INGESTED = "ingested"
    READY = "ready"

from enum import StrEnum

from openpyxl.utils import column_index_from_string
from pydantic import BaseModel, Field

from citadel.tabular.sheet_dump import SheetDump
from citadel.tabular.sheet_tools import RANGE


class RowRole(StrEnum):
    PROVENANCE = "provenance"
    TITLE = "title"
    CAPTION = "caption"
    HEADER = "header"
    BODY = "body"
    BAND_LABEL = "band_label"
    TOTALS = "totals"
    NOTE = "note"
    KEY_VALUE = "key_value"
    LEGEND = "legend"
    BLANK = "blank"
    UNRESOLVED = "unresolved"


class Orientation(StrEnum):
    ROW_RECORDS = "row_records"
    CROSS_TAB = "cross_tab"
    REPEATED_GROUPS = "repeated_groups"
    MATRIX = "matrix"


class Confidence(StrEnum):
    CORROBORATED = "corroborated"
    INFERRED = "inferred"
    UNCERTAIN = "uncertain"


class ColumnDef(BaseModel):
    letter: str
    index: int
    name: str
    header_parts: list[str] = Field(default_factory=list)
    group: str | None = None


class TableStructure(BaseModel):
    table_id: str
    extent: str
    caption: str | None = None
    header_rows: list[int] = Field(default_factory=list)
    body_rows: list[int] = Field(default_factory=list)
    totals_rows: list[int] = Field(default_factory=list)
    band_label_rows: list[int] = Field(default_factory=list)
    columns: list[ColumnDef] = Field(default_factory=list)
    orientation: Orientation = Orientation.ROW_RECORDS
    evidence: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.INFERRED


class NonTableBlock(BaseModel):
    kind: RowRole
    extent: str
    summary: str


class RowSpan(BaseModel):
    from_row: int
    to_row: int
    role: RowRole


class SheetExtraction(BaseModel):
    workbook: str
    sheet: str
    tables: list[TableStructure] = Field(default_factory=list)
    blocks: list[NonTableBlock] = Field(default_factory=list)
    row_roles: list[RowSpan] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


def assigned_rows(extraction: SheetExtraction) -> dict[int, RowRole]:
    out: dict[int, RowRole] = {}
    for span in extraction.row_roles:
        for row in range(min(span.from_row, span.to_row), max(span.from_row, span.to_row) + 1):
            out[row] = span.role
    return out


def extent_box(extent: str) -> tuple[int, int, int, int] | None:
    match = RANGE.match(extent.strip().upper().replace(" ", ""))
    if not match or not match.group(4):
        return None
    col_a = column_index_from_string(match.group(1))
    col_b = column_index_from_string(match.group(3))
    return (
        min(int(match.group(2)), int(match.group(4))),
        min(col_a, col_b),
        max(int(match.group(2)), int(match.group(4))),
        max(col_a, col_b),
    )


def extent_rows(extent: str) -> tuple[int, int] | None:
    box = extent_box(extent)
    return None if box is None else (box[0], box[2])


def boxes_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def coverage(dump: SheetDump, extraction: SheetExtraction) -> float:
    total = dump.last_row - dump.first_row + 1
    if total <= 0:
        return 1.0
    roles = assigned_rows(extraction)
    assigned = sum(1 for row in range(dump.first_row, dump.last_row + 1) if row in roles)
    return assigned / total


PREVIEW = 20


def spans(rows: list[int]) -> list[str]:
    out: list[list[int]] = []
    for row in sorted(rows):
        if out and row == out[-1][1] + 1:
            out[-1][1] = row
        else:
            out.append([row, row])
    return [str(a) if a == b else f"{a}-{b}" for a, b in out]


def validate_roles(dump: SheetDump, extraction: SheetExtraction) -> list[str]:
    problems: list[str] = [
        f"row span {span.from_row}-{span.to_row} is inverted"
        for span in extraction.row_roles
        if span.to_row < span.from_row
    ]
    roles = assigned_rows(extraction)
    missing = [row for row in range(dump.first_row, dump.last_row + 1) if row not in roles]
    if missing:
        preview = ", ".join(spans(missing)[:PREVIEW])
        problems.append(f"row_roles leaves {len(missing)} rows unassigned in extent {dump.extent}: {preview}")
    outside = sorted(row for row in roles if not dump.first_row <= row <= dump.last_row)
    if outside:
        problems.append(f"row_roles covers rows outside extent {dump.extent}: {spans(outside)[:PREVIEW]}")
    counted = sum(abs(span.to_row - span.from_row) + 1 for span in extraction.row_roles)
    if counted > len(roles):
        problems.append(f"row spans overlap: {counted} rows declared but {len(roles)} distinct")
    return problems


def validate_table(dump: SheetDump, table: TableStructure) -> list[str]:
    span = extent_rows(table.extent)
    if span is None:
        return [f"table {table.table_id} has an unparseable extent: {table.extent!r}"]
    start, end = span
    problems: list[str] = []
    if start < dump.first_row or end > dump.last_row:
        problems.append(f"table {table.table_id} extent {table.extent} falls outside sheet extent {dump.extent}")
    for label, rows in (
        ("header_rows", table.header_rows),
        ("body_rows", table.body_rows),
        ("totals_rows", table.totals_rows),
        ("band_label_rows", table.band_label_rows),
    ):
        stray = [row for row in rows if not start <= row <= end]
        if stray:
            problems.append(f"table {table.table_id} {label} outside its own extent {table.extent}: {stray[:PREVIEW]}")
    if not table.body_rows:
        problems.append(f"table {table.table_id} has no body rows")
    return problems


def validate_overlaps(extraction: SheetExtraction) -> list[str]:
    boxed = [(table.table_id, extent_box(table.extent)) for table in extraction.tables]
    known = [(name, box) for name, box in boxed if box is not None]
    return [
        f"tables {a} {extraction.tables[i].extent} and {b} {extraction.tables[j].extent} overlap"
        for i, (a, box_a) in enumerate(known)
        for j, (b, box_b) in enumerate(known)
        if i < j and boxes_overlap(box_a, box_b)
    ]


def validate_extraction(dump: SheetDump, extraction: SheetExtraction) -> list[str]:
    problems = validate_roles(dump, extraction)
    for table in extraction.tables:
        problems.extend(validate_table(dump, table))
    problems.extend(validate_overlaps(extraction))
    return problems

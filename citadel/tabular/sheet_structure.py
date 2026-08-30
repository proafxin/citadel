from enum import StrEnum

from openpyxl.utils import column_index_from_string
from pydantic import BaseModel, Field

from citadel.tabular.sheet_dump import SheetDump
from citadel.tabular.sheet_tools import AGGREGATE, RANGE


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


class RowRange(BaseModel):
    from_row: int
    to_row: int


class ColumnDef(BaseModel):
    letter: str
    name: str
    index: int = 0
    header_parts: list[str] = Field(default_factory=list)
    group: str | None = None


class TableStructure(BaseModel):
    table_id: str
    extent: str
    caption: str | None = None
    header_rows: list[int] = Field(default_factory=list)
    body_rows: list[RowRange] = Field(default_factory=list)
    totals_rows: list[int] = Field(default_factory=list)
    band_label_rows: list[int] = Field(default_factory=list)
    columns: list[ColumnDef] = Field(default_factory=list)
    group_name: str | None = None
    orientation: Orientation = Orientation.ROW_RECORDS
    evidence: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.INFERRED
    support: list[str] = Field(default_factory=list)


class NonTableBlock(BaseModel):
    kind: RowRole
    extent: str
    summary: str


class RowSpan(BaseModel):
    from_row: int
    to_row: int
    role: RowRole


class SheetTables(BaseModel):
    workbook: str = ""
    sheet: str = ""
    tables: list[TableStructure] = Field(default_factory=list)
    blocks: list[NonTableBlock] = Field(default_factory=list)
    row_roles: list[RowSpan] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    rounds: int = 0


def assigned_rows(extraction: SheetTables) -> dict[int, RowRole]:
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


def coverage(dump: SheetDump, extraction: SheetTables) -> float:
    total = dump.last_row - dump.first_row + 1
    if total <= 0:
        return 1.0
    roles = assigned_rows(extraction)
    assigned = sum(1 for row in range(dump.first_row, dump.last_row + 1) if row in roles)
    return assigned / total


PREVIEW = 20
RANGE_PAD = 1


def spans(rows: list[int]) -> list[str]:
    out: list[list[int]] = []
    for row in sorted(rows):
        if out and row == out[-1][1] + 1:
            out[-1][1] = row
        else:
            out.append([row, row])
    return [str(a) if a == b else f"{a}-{b}" for a, b in out]


def validate_roles(dump: SheetDump, extraction: SheetTables) -> list[str]:
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


def body_row_numbers(table: TableStructure) -> list[int]:
    return [
        row
        for span in table.body_rows
        for row in range(min(span.from_row, span.to_row), max(span.from_row, span.to_row) + 1)
    ]


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
        ("body_rows", body_row_numbers(table)),
        ("totals_rows", table.totals_rows),
        ("band_label_rows", table.band_label_rows),
    ):
        stray = [row for row in rows if not start <= row <= end]
        if stray:
            problems.append(f"table {table.table_id} {label} outside its own extent {table.extent}: {stray[:PREVIEW]}")
    if not table.body_rows:
        problems.append(f"table {table.table_id} has no body rows")
    return problems


def validate_overlaps(extraction: SheetTables) -> list[str]:
    boxed = [(table.table_id, extent_box(table.extent)) for table in extraction.tables]
    known = [(name, box) for name, box in boxed if box is not None]
    return [
        f"tables {a} {extraction.tables[i].extent} and {b} {extraction.tables[j].extent} overlap"
        for i, (a, box_a) in enumerate(known)
        for j, (b, box_b) in enumerate(known)
        if i < j and boxes_overlap(box_a, box_b)
    ]


TABLE_ROLES = frozenset({RowRole.BODY})


def validate_responsive(extraction: SheetTables) -> list[str]:
    roles = assigned_rows(extraction)
    tabular = sorted(row for row, role in roles.items() if role in TABLE_ROLES)
    if not tabular:
        return []
    if not extraction.tables:
        listed = ", ".join(spans(tabular)[:PREVIEW])
        return [f"row_roles marks rows {listed} as body rows but no tables were reported"]
    covered = {
        row
        for table in extraction.tables
        if (box := extent_box(table.extent)) is not None
        for row in range(box[0], box[2] + 1)
    }
    orphans = [row for row in tabular if row not in covered]
    if orphans:
        listed = ", ".join(spans(orphans)[:PREVIEW])
        return [f"rows {listed} are marked as body rows but fall inside no table extent"]
    return []


def validate_groups(table: TableStructure) -> list[str]:
    grouped = [column for column in table.columns if column.group]
    if not grouped:
        if table.orientation == Orientation.REPEATED_GROUPS:
            message = (
                f"table {table.table_id} is {Orientation.REPEATED_GROUPS.value} but no column carries a group; "
                "each repeat needs a group label that identifies it the way a primary key would"
            )
            return [message]
        return []
    sizes: dict[str, int] = {}
    for column in grouped:
        sizes[column.group or ""] = sizes.get(column.group or "", 0) + 1
    problems: list[str] = []
    repeats = len(sizes)
    if repeats > 1 and len({*sizes.values()}) > 1:
        shape = ", ".join(f"{name}={count}" for name, count in sorted(sizes.items()))
        problems.append(
            f"table {table.table_id} repeats are uneven ({shape}); every repeat of a group must span the same columns"
        )
    if repeats == 1 and len(grouped) > 1 and table.orientation == Orientation.REPEATED_GROUPS:
        problems.append(
            f"table {table.table_id} gives every column the same group {next(iter(sizes))!r}; "
            "a label shared by all repeats cannot identify them, choose the header row whose value differs per repeat"
        )
    return problems


def validate_extraction(dump: SheetDump, extraction: SheetTables) -> list[str]:
    problems = validate_roles(dump, extraction)
    for table in extraction.tables:
        problems.extend(validate_table(dump, table))
    problems.extend(validate_overlaps(extraction))
    return problems


def review_extraction(extraction: SheetTables) -> list[str]:
    notes = validate_responsive(extraction)
    for table in extraction.tables:
        notes.extend(validate_groups(table))
    return notes


def normalise_ref(ref: str) -> str:
    return ref.replace("$", "").replace("'", "").upper().split("!")[-1].strip()


def declared_ranges(dump: SheetDump) -> dict[str, str]:
    out = {normalise_ref(value): f"defined name {name}" for name, value in dump.meta.defined_names.items()}
    for merged in dump.meta.merged:
        out.setdefault(normalise_ref(merged), f"merged range {merged}")
    return out


def aggregate_ranges(dump: SheetDump) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in dump.cells.values():
        for cell in row.values():
            if cell.formula is None:
                continue
            for match in AGGREGATE.finditer(cell.formula):
                out[normalise_ref(match.group(2))] = f"{match.group(1)} at {cell.ref}"
    return out


def corroboration(dump: SheetDump, table: TableStructure) -> tuple[Confidence, list[str]]:
    box = extent_box(table.extent)
    if box is None:
        return Confidence.UNCERTAIN, []
    found: list[str] = []
    for ref, source in declared_ranges(dump).items():
        other = extent_box(ref)
        if other is not None and boxes_overlap(box, other) and (other[0], other[2]) == (box[0], box[2]):
            found.append(f"{source} matches rows {box[0]}-{box[2]}")
    rows = body_row_numbers(table)
    if rows:
        low, high = min(rows), max(rows)
        for ref, source in aggregate_ranges(dump).items():
            other = extent_box(ref)
            if other is None or not box[1] <= other[1] <= box[3]:
                continue
            if other[0] <= low <= other[0] + RANGE_PAD and other[2] - RANGE_PAD <= high <= other[2]:
                found.append(f"{source} covers body rows {low}-{high}")
    if found:
        return Confidence.CORROBORATED, found[:4]
    return Confidence.INFERRED, []


def score_extraction(dump: SheetDump, extraction: SheetTables) -> dict[str, int]:
    tally: dict[str, int] = {}
    for table in extraction.tables:
        level, support = corroboration(dump, table)
        table.confidence = level
        table.support = support
        tally[level.value] = tally.get(level.value, 0) + 1
    return tally


COMPUTED_FIELDS = {
    "SheetTables": ("workbook", "sheet", "rounds"),
    "TableStructure": ("confidence", "support"),
    "ColumnDef": ("index",),
}


def prune(node: dict, names: tuple[str, ...]) -> None:
    for name in names:
        node.get("properties", {}).pop(name, None)
        if name in node.get("required", []):
            node["required"].remove(name)


REQUIRED_FIELDS = {
    "SheetTables": ("tables", "blocks", "row_roles"),
    "TableStructure": ("table_id", "extent", "header_rows", "body_rows", "columns", "orientation", "group_name"),
    "ColumnDef": ("letter", "name", "group", "header_parts"),
}


MIN_ITEMS = {
    "SheetTables": {"row_roles": 1},
    "TableStructure": {"body_rows": 1, "columns": 1},
}


def set_min_items(node: dict, limits: dict[str, int]) -> None:
    properties = node.get("properties", {})
    for name, minimum in limits.items():
        field = properties.get(name)
        if isinstance(field, dict) and field.get("type") == "array":
            field["minItems"] = minimum


def require(node: dict, names: tuple[str, ...]) -> None:
    present = node.get("properties", {})
    required = node.setdefault("required", [])
    for name in names:
        if name in present and name not in required:
            required.append(name)


def request_schema() -> dict:
    schema = SheetTables.model_json_schema()
    prune(schema, COMPUTED_FIELDS["SheetTables"])
    require(schema, REQUIRED_FIELDS["SheetTables"])
    for name, fields in COMPUTED_FIELDS.items():
        target = schema.get("$defs", {}).get(name)
        if target is not None:
            prune(target, fields)
    for name, fields in REQUIRED_FIELDS.items():
        target = schema.get("$defs", {}).get(name)
        if target is not None:
            require(target, fields)
    set_min_items(schema, MIN_ITEMS["SheetTables"])
    for name, limits in MIN_ITEMS.items():
        target = schema.get("$defs", {}).get(name)
        if target is not None:
            set_min_items(target, limits)
    return schema


def fill_computed(dump: SheetDump, extraction: SheetTables) -> SheetTables:
    extraction.workbook = dump.workbook
    extraction.sheet = dump.sheet
    for table in extraction.tables:
        for column in table.columns:
            letters = "".join(ch for ch in column.letter.upper() if ch.isalpha())
            column.index = column_index_from_string(letters) if letters else 0
    return extraction

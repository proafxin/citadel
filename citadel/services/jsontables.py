import json

from citadel.schemas.table import CellValue, Column, ColumnDType
from citadel.services.excel import SAMPLE_TABLE_ROWS, MaterializedTable


def _next(counters: dict[str, int], entity: str) -> int:
    counters[entity] = counters.get(entity, 0) + 1
    return counters[entity]


def _scalar(value: object) -> CellValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return json.dumps(value, ensure_ascii=False)


def _process_field(
    key: str,
    value: object,
    row: dict,
    entity: str,
    row_id: int,
    entities: dict[str, list[dict]],
    counters: dict[str, int],
) -> None:
    if isinstance(value, dict):
        for subkey, subvalue in value.items():
            _process_field(f"{key}.{subkey}", subvalue, row, entity, row_id, entities, counters)
    elif isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        child = f"{entity}.{key}"
        for item in value:
            child_id = _next(counters, child)
            child_row: dict = {"_id": child_id, f"{entity}_id": row_id}
            for field_key, field_value in item.items():
                _process_field(field_key, field_value, child_row, child, child_id, entities, counters)
            entities.setdefault(child, []).append(child_row)
    else:
        row[key] = _scalar(value)


def normalize_json(data: object, root: str) -> dict[str, list[dict]]:
    entities: dict[str, list[dict]] = {}
    counters: dict[str, int] = {}
    records = data if isinstance(data, list) else [data]
    for record in records:
        fields = record if isinstance(record, dict) else {"value": record}
        row_id = _next(counters, root)
        row: dict = {"_id": row_id}
        for key, value in fields.items():
            _process_field(key, value, row, root, row_id, entities, counters)
        entities.setdefault(root, []).append(row)
    return entities


def _infer_dtype(values: list[CellValue]) -> ColumnDType:
    present = [value for value in values if value is not None]
    if not present:
        return ColumnDType.STRING
    if all(isinstance(value, bool) for value in present):
        return ColumnDType.BOOLEAN
    if all(isinstance(value, int) and not isinstance(value, bool) for value in present):
        return ColumnDType.INTEGER
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in present):
        return ColumnDType.FLOAT
    return ColumnDType.STRING


def _entity_to_table(name: str, rows: list[dict]) -> MaterializedTable:
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    columns = [Column(header=key, dtype=_infer_dtype([row.get(key) for row in rows])) for key in keys]
    data_rows = [[row.get(key) for key in keys] for row in rows]
    return MaterializedTable(
        sheet_no=0,
        columns=columns,
        rows=data_rows,
        sample_rows=data_rows[:SAMPLE_TABLE_ROWS],
        n_rows=len(data_rows),
        title=name,
        caption=None,
        notes=[],
        description="",
        anchors=None,
    )


def extract_json_tables(data: bytes, root: str) -> list[tuple[int, MaterializedTable]]:
    entities = normalize_json(json.loads(data), root)
    return [(ordinal, _entity_to_table(name, rows)) for ordinal, (name, rows) in enumerate(entities.items(), start=1)]

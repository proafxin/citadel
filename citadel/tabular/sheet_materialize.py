import logging
import time

from openpyxl.utils import column_index_from_string, get_column_letter

from citadel.schemas.table import TableStructure as GridStructure
from citadel.services.excel import (
    DUMP_VERSION,
    Cell,
    SheetExtraction,
    SheetItem,
    SheetText,
    cell_value,
    render_sheet_dump,
)
from citadel.tabular.materialize import MaterializedTable, materialize
from citadel.tabular.sheet_agent import extract_sheet, fits_context
from citadel.tabular.sheet_dump import SheetDump, parse_dump
from citadel.tabular.sheet_structure import (
    Orientation,
    SheetTables,
    TableStructure,
    body_row_numbers,
    extent_box,
)

logger = logging.getLogger(__name__)


def cell_index(cells: list[Cell]) -> dict[tuple[int, int], Cell]:
    return {(cell.row, cell.col): cell for cell in cells}


def render(cell: Cell | None) -> str:
    if cell is None:
        return ""
    value = cell_value(cell)
    return "" if value is None else str(value)


def table_rows(table: TableStructure, box: tuple[int, int, int, int]) -> list[int]:
    body = body_row_numbers(table)
    ordered = sorted({*table.header_rows, *body, *table.totals_rows, *table.band_label_rows})
    return [row for row in ordered if box[0] <= row <= box[2]]


def build_grid(index: dict[tuple[int, int], Cell], rows: list[int], first_col: int, last_col: int) -> list[list[str]]:
    return [[render(index.get((row, col))) for col in range(first_col, last_col + 1)] for row in rows]


DEFAULT_BAND_NAME = "section"


def band_target(table: TableStructure, box: tuple[int, int, int, int]) -> int | None:
    letters = (table.band_column or "").strip().upper()
    if not letters.isalpha():
        return None
    offset = column_index_from_string(letters) - box[1]
    return offset if 0 <= offset <= box[3] - box[1] else None


def band_for_rows(table: TableStructure) -> dict[int, str]:
    out: dict[int, str] = {}
    for band in table.bands:
        for row in range(min(band.from_row, band.to_row), max(band.from_row, band.to_row) + 1):
            out[row] = band.label
    return out


def grid_structure(table: TableStructure, rows: list[int], width: int) -> GridStructure | None:
    positions = {row: offset for offset, row in enumerate(rows)}
    header = [positions[row] for row in table.header_rows if row in positions]
    body = [positions[row] for row in body_row_numbers(table) if row in positions]
    if not body:
        return None
    extra = [positions[row] for row in (*table.totals_rows, *table.band_label_rows) if row in positions]
    return GridStructure(
        transposed=table.orientation == Orientation.MATRIX,
        col_start=0,
        col_end=width - 1,
        header_rows=header or None,
        data_start=min(body),
        data_end=max(body),
        metadata_rows=sorted(extra) or None,
        columns=[column.name for column in table.columns] or None,
        title=table.caption,
        caption=table.caption,
        notes=table.support or None,
    )


def anchors(table: TableStructure, box: tuple[int, int, int, int], tables: SheetTables) -> dict:
    return {
        "table_id": table.table_id,
        "extent": table.extent,
        "min_row": box[0],
        "min_col": box[1],
        "max_row": box[2],
        "max_col": box[3],
        "orientation": table.orientation.value,
        "confidence": table.confidence.value,
        "support": table.support,
        "sheet_rows": {
            "header": table.header_rows,
            "body": [[span.from_row, span.to_row] for span in table.body_rows],
            "totals": table.totals_rows,
            "band_label": table.band_label_rows,
        },
        "group_name": table.group_name,
        "band_name": table.band_name,
        "band_column": table.band_column,
        "bands": [[band.label, band.from_row, band.to_row] for band in table.bands],
        "rounds": tables.rounds,
        "unresolved": tables.unresolved,
        "dump_version": DUMP_VERSION,
    }


def apply_names(built: MaterializedTable, table: TableStructure, box: tuple[int, int, int, int]) -> None:
    by_letter = {column.letter.strip().upper(): column.name for column in table.columns if column.name}
    for offset, column in enumerate(built.columns):
        name = by_letter.get(get_column_letter(box[1] + offset))
        if name:
            column.header = name


def apply_groups(built: MaterializedTable, table: TableStructure, box: tuple[int, int, int, int]) -> None:
    by_index = {column.index: column.group for column in table.columns if column.group}
    by_letter = {column.letter.strip().upper(): column.group for column in table.columns if column.group}
    for offset, column in enumerate(built.columns):
        letter = get_column_letter(box[1] + offset)
        column.group = by_index.get(box[1] + offset) or by_letter.get(letter)


def materialize_sheet(
    sheet: SheetExtraction, dump: SheetDump, tables: SheetTables
) -> list[tuple[int, MaterializedTable]]:
    index = cell_index(sheet.cells)
    produced: list[tuple[int, MaterializedTable]] = []
    for order, table in enumerate(tables.tables, start=1):
        box = extent_box(table.extent)
        if box is None:
            logger.warning("skipping %s: unparseable extent %r", table.table_id, table.extent)
            continue
        rows = table_rows(table, box)
        if not rows:
            logger.warning("skipping %s: no rows inside extent %s", table.table_id, table.extent)
            continue
        grid = build_grid(index, rows, box[1], box[3])
        bands = band_for_rows(table)
        target = band_target(table, box)
        if bands:
            body = set(body_row_numbers(table))
            for offset, row in enumerate(rows):
                value = bands.get(row, "") if row in body else ""
                if target is None:
                    grid[offset] = [*grid[offset], value]
                elif value:
                    grid[offset][target] = value
        width = box[3] - box[1] + 1 + (1 if bands and band_target(table, box) is None else 0)
        structure = grid_structure(table, rows, width)
        if structure is None:
            logger.warning("skipping %s: no body rows inside extent %s", table.table_id, table.extent)
            continue
        formulas = sorted(
            {
                cell.formula
                for row in rows
                for col in range(box[1], box[3] + 1)
                if (cell := index.get((row, col))) is not None and cell.formula is not None
            }
        )
        built = materialize(
            grid,
            structure,
            sheet_no=sheet.sheet_no,
            formulas=formulas or None,
            extra_notes=[block.summary for block in tables.blocks] or None,
            anchors=anchors(table, box, tables),
        )
        apply_names(built, table, box)
        apply_groups(built, table, box)
        if bands and built.columns:
            position = band_target(table, box)
            slot = built.columns[position if position is not None else -1]
            slot.header = table.band_name or DEFAULT_BAND_NAME
            slot.group = None
        produced.append((order, built))
    return produced


async def structure_sheet(sheet: SheetExtraction, workbook: str) -> list[tuple[int, SheetItem]]:
    queued = time.time()
    text = render_sheet_dump(sheet, workbook)
    dump = parse_dump(text)
    if not dump.last_row:
        logger.info("%s/%s has no populated cells, nothing to structure", workbook, sheet.sheet_name)
        return []
    logger.info(
        "structuring %s/%s extent=%s cells=%d dump_chars=%d",
        workbook,
        sheet.sheet_name,
        dump.extent,
        len(sheet.cells),
        len(text),
    )
    fits, needed, allowed = fits_context(dump, text)
    if not fits:
        logger.warning(
            "skipping oversized sheet %s/%s: %d input tokens exceeds %d",
            workbook,
            sheet.sheet_name,
            needed,
            allowed,
        )
        return []
    started = time.time()
    tables = await extract_sheet(dump, text)
    elapsed = time.time() - started
    materialized = materialize_sheet(sheet, dump, tables)
    items: list[tuple[int, SheetItem]] = list(materialized)
    notes = "\n".join(f"{block.extent} {block.kind.value}: {block.summary}" for block in tables.blocks)
    if notes:
        items.append((len(items) + 1, SheetText(sheet_no=sheet.sheet_no, text=notes)))
    logger.info(
        "structured %s/%s in %.1fs (queued %.1fs): %d tables reported, %d materialized, %d rows, %d blocks",
        workbook,
        sheet.sheet_name,
        elapsed,
        started - queued,
        len(tables.tables),
        len(materialized),
        sum(table.n_rows for _, table in materialized),
        len(tables.blocks),
    )
    if tables.unresolved:
        logger.warning("%s/%s left unresolved: %s", workbook, sheet.sheet_name, ", ".join(tables.unresolved[:5]))
    return items

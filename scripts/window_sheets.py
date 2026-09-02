import asyncio
import logging
import sys
from pathlib import Path

from pydantic import BaseModel

from citadel.llm import count_tokens
from citadel.services.excel import SheetExtraction, load_all_sheets, render_sheet_dump
from citadel.tabular.sheet_agent import AgentTrace, extract_sheet
from citadel.tabular.sheet_dump import parse_dump
from citadel.tabular.sheet_structure import SheetStructure, coverage
from citadel.tabular.sheet_window import Window, plan_windows
from config import configure_logging

logger = logging.getLogger(__name__)

UNSAFE = str.maketrans({"/": "_", "\\": "_", " ": "_", ":": "_", "*": "_", "?": "_", "[": "_", "]": "_", '"': "_"})


class WindowRun(BaseModel):
    workbook: str
    sheet: str
    window: Window
    structure: SheetStructure
    coverage: float


async def run_window(
    sheet: SheetExtraction, workbook: str, window: Window, out_dir: Path, stem: str
) -> WindowRun:
    text = render_sheet_dump(sheet, workbook, window.first_row, window.last_row)
    dump = parse_dump(text)
    name = f"{stem}_w{window.index:02d}_rows{window.first_row}-{window.last_row}"
    (out_dir / f"{name}.md").write_text(text, encoding="utf-8")
    trace = AgentTrace()
    structure = await extract_sheet(dump, text, trace=trace)
    (out_dir / f"{name}.json").write_text(structure.model_dump_json(indent=2), encoding="utf-8")
    (out_dir / f"{name}.trace.json").write_text(trace.model_dump_json(indent=2), encoding="utf-8")
    return WindowRun(
        workbook=workbook,
        sheet=sheet.sheet_name,
        window=window,
        structure=structure,
        coverage=coverage(dump, structure),
    )


def kept(results: list[object], label: str) -> list:
    done = []
    for item in results:
        if isinstance(item, BaseException):
            logger.error("%s failed: %s: %s", label, type(item).__name__, item)
        else:
            done.append(item)
    return done


async def run_sheet(sheet: SheetExtraction, workbook: str, out_dir: Path) -> list[WindowRun]:
    text = render_sheet_dump(sheet, workbook)
    dump = parse_dump(text)
    if not dump.last_row:
        logger.info("%s/%s has no populated cells", workbook, sheet.sheet_name)
        return []
    windows = plan_windows(dump, text)
    stem = f"{Path(workbook).stem}__{sheet.sheet_no:02d}_{sheet.sheet_name.translate(UNSAFE)}"
    logger.info(
        "%s/%s %d tokens over rows %d-%d -> %d window(s): %s",
        workbook,
        sheet.sheet_name,
        count_tokens(text),
        dump.first_row,
        dump.last_row,
        len(windows),
        ", ".join(f"{w.first_row}-{w.last_row}({w.tokens})" for w in windows),
    )
    results = await asyncio.gather(
        *(run_window(sheet, workbook, window, out_dir, stem) for window in windows), return_exceptions=True
    )
    return kept(list(results), f"{workbook}/{sheet.sheet_name}")


async def run_workbook(path: Path, out_dir: Path) -> list[WindowRun]:
    sheets = load_all_sheets(path.read_bytes())
    runs = await asyncio.gather(
        *(run_sheet(sheet, path.name, out_dir) for sheet in sheets), return_exceptions=True
    )
    return [run for group in kept(list(runs), path.name) for run in group]


def report(runs: list[WindowRun]) -> None:
    for run in runs:
        logger.info(
            "%-28s %-22s w%02d rows %5d-%-5d regions=%2d metadata=%2d rows=%4d coverage=%.3f rounds=%d",
            run.workbook,
            run.sheet,
            run.window.index,
            run.window.first_row,
            run.window.last_row,
            len(run.structure.regions),
            len(run.structure.metadata),
            sum(len(region.row_spans) for region in run.structure.regions),
            run.coverage,
            run.structure.rounds,
        )
        for region in run.structure.regions:
            logger.info(
                "    %-4s rows %5d-%-5d cols %3d-%-3d %-14s before=%-5s after=%-5s key=%s",
                region.region_id,
                region.first_row,
                region.last_row,
                region.first_col,
                region.last_col,
                region.orientation.value,
                region.continues_before,
                region.continues_after,
                region.key_column,
            )
        if run.structure.unresolved:
            logger.warning("    unresolved: %s", "; ".join(run.structure.unresolved[:3]))


async def main() -> None:
    configure_logging()
    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    groups = await asyncio.gather(
        *(run_workbook(Path(raw), out_dir) for raw in sys.argv[2:]), return_exceptions=True
    )
    runs = [run for group in kept(list(groups), "workbook") for run in group]
    report(runs)
    logger.info("wrote %d window runs to %s", len(runs), out_dir)


if __name__ == "__main__":
    asyncio.run(main())

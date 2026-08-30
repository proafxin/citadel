import asyncio
import json
import logging
import pathlib
import sys

from citadel.services.excel import load_all_sheets
from citadel.tabular.agent import SheetResult, structure_sheet

logger = logging.getLogger("agent_run")

RUNS = pathlib.Path("data/agent_runs")


def _persist(work: pathlib.Path, result: SheetResult) -> None:
    work.mkdir(parents=True, exist_ok=True)
    for entry in result.log:
        (work / f"step{entry.step:03d}.prompt.md").write_text(entry.prompt)
        (work / f"step{entry.step:03d}.response.json").write_text(json.dumps(entry.action, indent=1))
    (work / "result.json").write_text(result.model_dump_json(indent=1, exclude={"log"}))


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    path = pathlib.Path(sys.argv[1])
    wanted = {int(arg) for arg in sys.argv[2:]}
    data = await asyncio.to_thread(path.read_bytes)
    for sheet in load_all_sheets(data):
        if not sheet.cells or (wanted and sheet.sheet_no not in wanted):
            continue
        result = await structure_sheet(sheet, key=f"agent:{path.stem}:sheet{sheet.sheet_no}")
        work = RUNS / f"{path.stem}_sheet{sheet.sheet_no}"
        await asyncio.to_thread(_persist, work, result)
        logger.info(
            "\n===== %s sheet%d %r =====\nsteps=%d tables=%d unclaimed=%d -> %s",
            path.name,
            sheet.sheet_no,
            sheet.sheet_name,
            result.steps,
            len(result.tables),
            result.unclaimed,
            work,
        )
        for index, spec in enumerate(result.tables):
            logger.info("  [%d] %s", index, json.dumps(spec.model_dump()))
        for problem in result.problems:
            logger.info("  PROBLEM %s", problem)


if __name__ == "__main__":
    asyncio.run(main())

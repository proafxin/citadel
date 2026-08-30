import json
import logging
import pathlib
import sys

from citadel.services.excel import SheetExtraction, load_all_sheets
from citadel.tabular.agent import AgentState, apply_action, finish, next_prompt

logger = logging.getLogger("agent_step")


def _sheet(path: pathlib.Path, sheet_no: int) -> SheetExtraction:
    for sheet in load_all_sheets(path.read_bytes()):
        if sheet.sheet_no == sheet_no:
            return sheet
    message = f"sheet {sheet_no} not found in {path.name}"
    raise ValueError(message)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    path = pathlib.Path(sys.argv[1])
    sheet_no = int(sys.argv[2])
    work = pathlib.Path(sys.argv[3])
    work.mkdir(parents=True, exist_ok=True)
    sheet = _sheet(path, sheet_no)

    state_file = work / "state.json"
    prompt_file = work / "prompt.md"
    response_file = work / "response.json"
    result_file = work / "result.json"

    state = AgentState.model_validate_json(state_file.read_text()) if state_file.exists() else AgentState()

    if response_file.exists():
        action = json.loads(response_file.read_text())
        apply_action(sheet, state, action)
        step = state.steps
        (work / f"step{step:03d}.response.json").write_text(json.dumps(action, indent=1))
        (work / f"step{step:03d}.state.json").write_text(state.model_dump_json(indent=1))
        response_file.unlink()

    prompt = next_prompt(sheet, state)
    state_file.write_text(state.model_dump_json(indent=1))

    if prompt is None:
        result = finish(sheet, state)
        result_file.write_text(result.model_dump_json(indent=1))
        prompt_file.unlink(missing_ok=True)
        logger.info("DONE %s tables=%d unclaimed=%d", state.stopped, len(result.tables), result.unclaimed)
        for problem in result.problems:
            logger.info("  PROBLEM %s", problem)
        return

    prompt_file.write_text(prompt)
    (work / f"step{state.steps + 1:03d}.prompt.md").write_text(prompt)
    logger.info("PROMPT step=%d chars=%d -> %s", state.steps, len(prompt), prompt_file)


if __name__ == "__main__":
    main()

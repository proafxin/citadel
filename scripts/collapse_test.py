import asyncio
import json
import logging
import pathlib
import sys

from citadel.llm import call_structured
from citadel.prompts import load_prompt

logger = logging.getLogger("collapse")

BASELINE = pathlib.Path("data/model_baselines/baseline_qwen38_27b_q4km/excel")

_SCHEMA = {
    "type": "object",
    "properties": {
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_row": {"type": "integer"},
                    "end_row": {"type": "integer"},
                    "start_col": {"type": "integer"},
                    "end_col": {"type": "integer"},
                    "header_rows": {"type": "array", "items": {"type": "integer"}},
                    "data_start": {"type": "integer"},
                    "data_end": {"type": "integer"},
                    "metadata_rows": {"type": "array", "items": {"type": "integer"}},
                    "title_row": {"type": ["integer", "null"]},
                },
                "required": [
                    "start_row",
                    "end_row",
                    "start_col",
                    "end_col",
                    "header_rows",
                    "data_start",
                    "data_end",
                    "metadata_rows",
                    "title_row",
                ],
            },
        }
    },
    "required": ["tables"],
}


def _split(path: pathlib.Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    body = text.split("## input\n```\n", 1)[1]
    grid_text, rest = body.split("\n```\n\n## model output\n", 1)
    return grid_text, rest.strip()


async def _run(path: pathlib.Path) -> tuple[str, dict]:
    grid_text, description = _split(path)
    prompt = f"{load_prompt('table_extract_all')}\n\n{grid_text}\n\n{description}"
    return path.stem, await call_structured(prompt, _SCHEMA)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    targets = tuple(sys.argv[1:])
    paths = sorted(p for p in BASELINE.glob("*.md") if not targets or p.stem in targets)
    for label, data in await asyncio.gather(*(_run(path) for path in paths)):
        tables = data.get("tables") or []
        logger.info("%-46s n=%d", label, len(tables))
        for index, table in enumerate(tables):
            logger.info(
                "    [%d] box=(%s,%s,%s,%s) headers=%s data=%s..%s meta=%s title_row=%s",
                index,
                table.get("start_row"),
                table.get("end_row"),
                table.get("start_col"),
                table.get("end_col"),
                table.get("header_rows"),
                table.get("data_start"),
                table.get("data_end"),
                table.get("metadata_rows"),
                table.get("title_row"),
            )
        logger.debug("%s raw=%s", label, json.dumps(data))


if __name__ == "__main__":
    asyncio.run(main())

import logging
import sys
from pathlib import Path

from citadel.llm import count_tokens
from citadel.services.excel import load_all_sheets, render_sheet_dump
from config import configure_logging

logger = logging.getLogger(__name__)

UNSAFE = str.maketrans({"/": "_", "\\": "_", " ": "_", ":": "_", "*": "_", "?": "_", "[": "_", "]": "_", '"': "_"})
SLOT_BUDGETS = (16384, 32768, 65536)


def parse_window(arg: str) -> tuple[int, int]:
    first, _, last = arg.partition("-")
    return int(first), int(last)


def dump_workbook(path: Path, out_dir: Path, window: tuple[int, int] | None = None) -> list[tuple[str, int]]:
    counts: list[tuple[str, int]] = []
    suffix = f"_rows{window[0]}-{window[1]}" if window else ""
    for sheet in load_all_sheets(path.read_bytes()):
        text = render_sheet_dump(sheet, path.name, *(window or (None, None)))
        name = sheet.sheet_name.translate(UNSAFE)
        target = out_dir / f"{path.stem}__{sheet.sheet_no:02d}_{name}{suffix}.md"
        target.write_text(text, encoding="utf-8")
        tokens = count_tokens(text)
        counts.append((target.name, tokens))
        logger.info("%-44s %7d tokens %8d chars %6d cells", target.name, tokens, len(text), len(sheet.cells))
    return counts


def report(counts: list[tuple[str, int]]) -> None:
    if not counts:
        return
    tokens = sorted(size for _, size in counts)
    logger.info("sheets=%d min=%d median=%d max=%d", len(tokens), tokens[0], tokens[len(tokens) // 2], tokens[-1])
    for budget in SLOT_BUDGETS:
        fitting = sum(1 for size in tokens if size <= budget)
        logger.info("fit in %6d tokens: %d/%d sheets", budget, fitting, len(tokens))
    for name, size in sorted(counts, key=lambda row: -row[1])[:10]:
        logger.info("largest %-44s %7d tokens", name, size)


def main() -> None:
    configure_logging()
    out_dir = Path(sys.argv[1])
    args = sys.argv[2:]
    window = parse_window(args.pop(0)) if args and args[0][0].isdigit() else None
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = [row for raw in args for row in dump_workbook(Path(raw), out_dir, window)]
    logger.info("wrote %d sheet dumps to %s", len(counts), out_dir)
    report(counts)


if __name__ == "__main__":
    main()

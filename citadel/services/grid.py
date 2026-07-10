import re

_LATEX_MARKERS = ("\\(", "\\[", "\\frac", "\\sum", "\\int", "\\sqrt", "\\left", "\\leq", "\\geq", "$$")
_MATH_CHARS = frozenset("∑∫≤≥≠±∞√αβγδεφζηθλμπσωτχψ→∈∀∃∂∇⊂⊆∪∩·×÷")
_EQ_NUMBER = re.compile(r"^\(\d+\)$")


def _is_math_cell(cell: str) -> bool:
    value = cell.strip()
    if any(marker in value for marker in _LATEX_MARKERS) or _EQ_NUMBER.match(value):
        return True
    return sum(1 for char in value if char in _MATH_CHARS) >= 2


def classify_grid(grid: list[list[str]]) -> str:
    cells = [cell for row in grid for cell in row if cell.strip()]
    if not cells:
        return "empty"
    if all(_is_math_cell(cell) for cell in cells):
        return "equation"
    if max(len(row) for row in grid) <= 1:
        return "prose"
    return "table"


def grid_text(grid: list[list[str]]) -> str:
    return " ".join(cell for row in grid for cell in row if cell.strip())

import re
import unicodedata
from dataclasses import dataclass, field

NULL_EQUIVALENTS = frozenset(
    {"", "-", "--", "n/a", "na", "nan", "nil", "none", "null", "nd", "n.d.", "sans objet", "tiada", "k.a."}
)
AGGREGATION_KEYWORDS = frozenset({"total", "subtotal", "sum", "grand total", "jumlah", "summe", "gesamt"})
FOOTNOTE_KEYWORDS = frozenset({"note", "notes", "source", "sources", "footnote"})

_THOUSANDS = re.compile(r"^-?\d{1,3}([ ,.]\d{3})+([.,]\d+)?$")
_DECIMAL = re.compile(r"^-?\d+[.,]\d+$")
_INTEGER = re.compile(r"^-?\d+$")


def _symbol(char: str) -> str:
    if char.isdigit():
        return "D"
    if char.isalpha():
        return "A"
    if char.isspace():
        return "W"
    return "S"


def clean(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    return normalized.strip("\"'")


def is_null(value: str) -> bool:
    return clean(value).casefold() in NULL_EQUIVALENTS


def is_numeric(value: str) -> bool:
    text = clean(value)
    return bool(_INTEGER.fullmatch(text) or _DECIMAL.fullmatch(text) or _THOUSANDS.fullmatch(text))


def is_strict_numeric(value: str) -> bool:
    text = clean(value)
    return bool(_INTEGER.fullmatch(text) or _DECIMAL.fullmatch(text))


def symbol_sequence(value: str) -> tuple[tuple[str, int], ...]:
    runs: list[tuple[str, int]] = []
    for char in clean(value):
        symbol = _symbol(char)
        if runs and runs[-1][0] == symbol:
            runs[-1] = (symbol, runs[-1][1] + 1)
        else:
            runs.append((symbol, 1))
    return tuple(runs)


def case_class(value: str) -> str:
    text = clean(value)
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return "none"
    if "_" in text and text == text.lower():
        return "snake"
    if all(char.isupper() for char in letters):
        return "upper"
    if all(char.islower() for char in letters):
        return "lower"
    if text == text.title():
        return "title"
    return "mixed"


@dataclass(frozen=True)
class CellSignature:
    value: str
    symbols: tuple[tuple[str, int], ...]
    symbols_reversed: tuple[tuple[str, int], ...]
    symbol_set: frozenset[str]
    case: str
    length: int
    numeric: bool
    strict_numeric: bool
    null: bool


def cell_signature(value: str) -> CellSignature:
    text = clean(value)
    symbols = symbol_sequence(value)
    return CellSignature(
        value=text,
        symbols=symbols,
        symbols_reversed=tuple(reversed(symbols)),
        symbol_set=frozenset(symbol for symbol, _ in symbols),
        case=case_class(value),
        length=len(text),
        numeric=is_numeric(value),
        strict_numeric=is_strict_numeric(value),
        null=is_null(value),
    )


def common_symbol_prefix(sequences: list[tuple[tuple[str, int], ...]]) -> tuple[str, ...]:
    if not sequences:
        return ()
    prefix: list[str] = []
    for position in range(min(len(sequence) for sequence in sequences)):
        symbols = {sequence[position][0] for sequence in sequences}
        prefix.append(next(iter(symbols)) if len(symbols) == 1 else "*")
    return tuple(prefix)


@dataclass
class LineSignature:
    cells: list[CellSignature]
    non_null: list[CellSignature] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.non_null = [cell for cell in self.cells if not cell.null]

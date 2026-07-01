import re

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from citadel.schemas.content import Block

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_INLINE = {
    "span",
    "a",
    "b",
    "i",
    "em",
    "strong",
    "u",
    "small",
    "sub",
    "sup",
    "mark",
    "abbr",
    "cite",
    "q",
    "time",
    "code",
    "kbd",
    "samp",
    "var",
    "s",
    "del",
    "ins",
    "tt",
    "font",
    "label",
    "br",
}


def _text(element: Tag) -> str:
    return element.get_text(separator=" ", strip=True)


def _flush(buffer: list[str], blocks: list[Block]) -> None:
    text = re.sub(r"\s+", " ", " ".join(buffer)).strip()
    buffer.clear()
    if text:
        blocks.append(Block(page_idx=0, type="text", text=text))


def _li_text(item: Tag) -> str:
    parts: list[str] = []
    for child in item.children:
        if isinstance(child, Tag):
            if child.name in {"ul", "ol"}:
                continue
            text = child.get_text(separator=" ", strip=True)
        else:
            text = str(child).strip()
        if text:
            parts.append(text)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _walk_list(element: Tag, depth: int, blocks: list[Block]) -> None:
    for item in element.find_all("li", recursive=False):
        text = _li_text(item)
        if text:
            blocks.append(Block(page_idx=0, type="list_item", text=text, text_level=depth))
        for nested in item.find_all(("ul", "ol"), recursive=False):
            _walk_list(nested, depth + 1, blocks)


def _int_attr(value: object) -> int | None:
    match = re.match(r"\d+", str(value)) if value is not None else None
    return int(match.group()) if match else None


def _keep_image(element: Tag) -> bool:
    if (element.get("role") or "").lower() == "presentation" or element.get("aria-hidden") == "true":
        return False
    width = _int_attr(element.get("width"))
    height = _int_attr(element.get("height"))
    return not (width is not None and height is not None and width < 32 and height < 32)


def _latex(element: Tag) -> str:
    annotation = element.find("annotation", attrs={"encoding": "application/x-tex"})
    if annotation is not None:
        return annotation.get_text().strip()
    return element.get_text(separator=" ", strip=True)


def _walk(element: Tag, blocks: list[Block]) -> None:
    buffer: list[str] = []
    for child in element.children:
        if isinstance(child, NavigableString):
            piece = str(child).strip()
            if piece:
                buffer.append(piece)
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name
        if name in _HEADINGS:
            _flush(buffer, blocks)
            text = _text(child)
            if text:
                blocks.append(Block(page_idx=0, type="title", text=text, text_level=_HEADINGS[name]))
        elif name == "p":
            _flush(buffer, blocks)
            text = _text(child)
            if text:
                blocks.append(Block(page_idx=0, type="text", text=text))
        elif name in {"ul", "ol"}:
            _flush(buffer, blocks)
            _walk_list(child, 0, blocks)
        elif name == "pre":
            _flush(buffer, blocks)
            if child.get_text().strip():
                blocks.append(Block(page_idx=0, type="code", text=child.get_text()))
        elif name == "table":
            _flush(buffer, blocks)
            blocks.append(Block(page_idx=0, type="table", text=str(child)))
        elif name == "math":
            _flush(buffer, blocks)
            latex = _latex(child)
            if latex:
                blocks.append(Block(page_idx=0, type="equation", text=latex))
        elif name == "img":
            if _keep_image(child):
                _flush(buffer, blocks)
                blocks.append(Block(page_idx=0, type="image", text=(child.get("alt") or "").strip()))
        elif name in _INLINE:
            piece = child.get_text(separator=" ", strip=True)
            if piece:
                buffer.append(piece)
        else:
            _flush(buffer, blocks)
            _walk(child, blocks)
    _flush(buffer, blocks)


def parse_html(data: bytes) -> list[Block]:
    soup = BeautifulSoup(data, "lxml")
    blocks: list[Block] = []
    _walk(soup.body or soup, blocks)
    return blocks

import re

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from citadel.schemas.content import Block

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_INLINE = {
    "span", "a", "b", "i", "em", "strong", "u", "small", "sub", "sup", "mark", "abbr", "cite", "q",
    "time", "code", "kbd", "samp", "var", "s", "del", "ins", "tt", "font", "label", "br",
}


def _text(element: Tag) -> str:
    return element.get_text(separator=" ", strip=True)


def _flush(buffer: list[str], blocks: list[Block]) -> None:
    text = re.sub(r"\s+", " ", " ".join(buffer)).strip()
    buffer.clear()
    if text:
        blocks.append(Block(page_idx=0, type="text", text=text))


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
        elif name in ("ul", "ol"):
            _flush(buffer, blocks)
            for item in child.find_all("li", recursive=False):
                text = _text(item)
                if text:
                    blocks.append(Block(page_idx=0, type="list_item", text=text))
        elif name == "pre":
            _flush(buffer, blocks)
            if child.get_text().strip():
                blocks.append(Block(page_idx=0, type="code", text=child.get_text()))
        elif name == "table":
            _flush(buffer, blocks)
            blocks.append(Block(page_idx=0, type="table", text=str(child)))
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

from bs4 import BeautifulSoup
from bs4.element import Tag

from citadel.schemas.content import Block

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


def _text(element: Tag) -> str:
    return element.get_text(separator=" ", strip=True)


def _walk(element: Tag, blocks: list[Block]) -> None:
    for child in element.children:
        if not isinstance(child, Tag):
            continue
        name = child.name
        if name in _HEADINGS:
            text = _text(child)
            if text:
                blocks.append(Block(page_idx=0, type="title", text=text, text_level=_HEADINGS[name]))
        elif name == "p":
            text = _text(child)
            if text:
                blocks.append(Block(page_idx=0, type="text", text=text))
        elif name in ("ul", "ol"):
            for item in child.find_all("li", recursive=False):
                text = _text(item)
                if text:
                    blocks.append(Block(page_idx=0, type="list_item", text=text))
        elif name == "pre":
            if child.get_text().strip():
                blocks.append(Block(page_idx=0, type="code", text=child.get_text()))
        elif name == "table":
            blocks.append(Block(page_idx=0, type="table", text=str(child)))
        else:
            _walk(child, blocks)


def parse_html(data: bytes) -> list[Block]:
    soup = BeautifulSoup(data, "lxml")
    blocks: list[Block] = []
    _walk(soup.body or soup, blocks)
    return blocks

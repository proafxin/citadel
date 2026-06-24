from dataclasses import dataclass, field


@dataclass(slots=True)
class Block:
    type: str  # text | title | table | image | equation | list | footer | header | page_number | ...
    page_idx: int
    bbox: list[int] = field(default_factory=list)
    text: str = ""
    text_level: int | None = None
    list_items: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ParsedDocument:
    source: str
    markdown: str
    blocks: list[Block]

    @property
    def page_count(self) -> int:
        return max((b.page_idx for b in self.blocks), default=-1) + 1

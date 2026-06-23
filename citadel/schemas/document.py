from dataclasses import dataclass


@dataclass(slots=True)
class PageText:
    page_number: int
    text: str
    truncated: bool = False


@dataclass(slots=True)
class DocumentText:
    source: str
    pages: list[PageText]

    @property
    def full_text(self) -> str:
        return "\n\n".join(page.text for page in self.pages)

    @property
    def truncated(self) -> bool:
        return any(page.truncated for page in self.pages)

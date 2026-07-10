from html import escape
from io import BytesIO

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from citadel.schemas.content import Block

_DRAWINGML_TEXT = "{http://schemas.openxmlformats.org/drawingml/2006/main}t"


def _bbox(shape: object) -> list[float] | None:
    left, top = getattr(shape, "left", None), getattr(shape, "top", None)
    if left is None or top is None:
        return None
    width = getattr(shape, "width", None) or 0
    height = getattr(shape, "height", None) or 0
    return [float(left), float(top), float(left + width), float(top + height)]


def _ordered(shapes: object) -> list:
    return sorted(shapes, key=lambda shape: (shape.top or 0, shape.left or 0))


def _cell_tag(row_index: int, first_row_is_header: bool) -> str:
    return "th" if row_index == 0 and first_row_is_header else "td"


def _table_html(table: object) -> str:
    rows: list[str] = []
    for row_index, row in enumerate(table.rows):
        tag = _cell_tag(row_index, bool(table.first_row))
        cells = "".join(f"<{tag}>{escape(cell.text.strip())}</{tag}>" for cell in row.cells)
        rows.append(f"<tr>{cells}</tr>")
    return "<table>" + "".join(rows) + "</table>"


def _series_cell(values: list, index: int) -> str:
    if index >= len(values) or values[index] is None:
        return "<td></td>"
    return f"<td>{escape(str(values[index]))}</td>"


def _chart_html(chart: object) -> str:
    categories = [str(category) for category in chart.plots[0].categories] if chart.plots else []
    series = list(chart.series)
    header = "".join(f"<th>{escape(str(item.name or ''))}</th>" for item in series)
    values = [list(item.values) for item in series]
    rows = [f"<tr><th>category</th>{header}</tr>"]
    for index, category in enumerate(categories):
        cells = "".join(_series_cell(column, index) for column in values)
        rows.append(f"<tr><td>{escape(category)}</td>{cells}</tr>")
    return "<table>" + "".join(rows) + "</table>"


def _alt_text(shape: object) -> str:
    for element in shape._element.iter():
        if element.tag.endswith("}cNvPr"):
            return (element.get("descr") or "").strip()
    return ""


def _graphic_text(shape: object) -> str:
    parts = [node.text.strip() for node in shape._element.iter(_DRAWINGML_TEXT) if node.text and node.text.strip()]
    return " ".join(parts)


def _text_frame_blocks(shape: object, slide_index: int, is_title: bool) -> list[Block]:
    bbox = _bbox(shape)
    if is_title:
        text = shape.text_frame.text.strip()
        return [Block(page_idx=slide_index, type="title", text=text, text_level=1, bbox=bbox)] if text else []
    blocks: list[Block] = []
    for paragraph in shape.text_frame.paragraphs:
        text = paragraph.text.strip()
        if text:
            blocks.append(Block(page_idx=slide_index, type="text", text=text, bbox=bbox))
    return blocks


def _shape_blocks(shape: object, slide_index: int, title_id: int | None) -> list[Block]:
    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        return [block for member in _ordered(shape.shapes) for block in _shape_blocks(member, slide_index, title_id)]
    if shape.has_table:
        return [Block(page_idx=slide_index, type="table", text=_table_html(shape.table), bbox=_bbox(shape))]
    if shape.has_chart:
        return [Block(page_idx=slide_index, type="table", text=_chart_html(shape.chart), bbox=_bbox(shape))]
    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
        return [Block(page_idx=slide_index, type="image", text=_alt_text(shape), bbox=_bbox(shape))]
    if shape.has_text_frame:
        return _text_frame_blocks(shape, slide_index, shape.shape_id == title_id)
    graphic = _graphic_text(shape)
    return [Block(page_idx=slide_index, type="text", text=graphic, bbox=_bbox(shape))] if graphic else []


def _notes_blocks(slide: object, slide_index: int) -> list[Block]:
    if not slide.has_notes_slide:
        return []
    notes = slide.notes_slide.notes_text_frame.text.strip()
    return [Block(page_idx=slide_index, type="text", text=notes)] if notes else []


def _slide_blocks(slide: object, slide_index: int) -> list[Block]:
    title = slide.shapes.title
    title_id = title.shape_id if title is not None else None
    blocks = [block for shape in _ordered(slide.shapes) for block in _shape_blocks(shape, slide_index, title_id)]
    return blocks + _notes_blocks(slide, slide_index)


def parse_pptx(data: bytes) -> list[list[Block]]:
    presentation = Presentation(BytesIO(data))
    return [_slide_blocks(slide, index) for index, slide in enumerate(presentation.slides)]

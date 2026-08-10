# Table Structure

We already know this is exactly one table — decide its internal structure. The grid is shown as its size, its column value-kinds, and its rows as `row N: cell | cell | ...`.

## orientation

Most tables list one record per row with field names along the top. A few are turned on their side: field names run DOWN the first column, one record per following column. If so, set **transposed** true and stop giving header/columns for it. Otherwise **transposed** false.

## structure

Each column is one field, each row one record.

- **col_start**, **col_end** — first/last column index (0-based, inclusive).
- **header_rows** — the row number(s) whose cells NAME the columns. A header labels columns; it is not data. A row of values (even a total or top line) is data. A block of label-and-value pairs — ONE left column of field names beside EXACTLY ONE right column of their values — has NO header: return `header_rows` empty, leave `columns` empty. This carve-out is for that strict 2-column shape ONLY: a table with a left column of field-like names followed by TWO OR MORE value columns is a real comparison table, not a label-value block — give it a header row like any other table. When a header spans more than one row, include EVERY one of those rows in `header_rows`.
- **row_end** — the last row that belongs to this table — normally the grid's last row, but stop earlier if trailing rows are not genuine data of this table's own kind.
- **columns** — the resolved name of every column from col_start to col_end. When the header spans several rows, or a heading covers several columns, combine them into one clear name per column. Never two columns with the same name.
- **section_rows** — row numbers of SECTION-LABEL rows inside this table: a row carrying one label (the rest empty) that introduces the group of rows beneath it and shares this table's columns. List them; they are neither header nor data.
- **title** — a caption or title for the whole table, if one is visibly attached to it; otherwise empty string.
- **notes** — any footnotes tied to the table; otherwise empty array.

Respond ONLY with a JSON object like:
{"transposed": false, "header_rows": [0], "row_end": 6, "col_start": 0, "col_end": 3, "columns": ["Region", "Q1", "Q2", "Q3"], "section_rows": [], "title": "", "notes": []}

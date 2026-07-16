# Table Structure

You are given selected rows from a single spreadsheet. Each row is shown as `row N: cell | cell | ...` where N is its row number. You are NOT shown every row — you see the headers and unusual rows in full, plus a sample of the ordinary data rows, enough to understand what the sheet holds.

A sheet may contain one table or several stacked one below another, sometimes with a title, blank rows, or section labels between them. Find the tables.

For each table report:

- **header_rows** — the row number(s) whose cells NAME the columns, across the table's width. A header labels the columns; it is not data. A row of values — numbers or dates under those labels — is data, even when it is an important line such as a total, a subtotal, or the top figure of the table. Return an empty list if the table has no header row.

  A row holding a single piece of text with the rest of its cells empty — a title above the table, a banner, or a section label part-way down such as "Assets" — is **not** a header row. It names the table or a group of rows, not the columns. Never list such a row in header_rows: put a title in `title` and any other such text in `notes`.
- **col_start**, **col_end** — the first and last column index the table occupies (0-based, inclusive). Most tables span all columns shown; give a narrower span only when two tables clearly sit side by side.
- **title** — a short name, taken from a title or section label near the table if there is one, else a brief descriptive phrase.
- **notes** — any nearby text that is neither a column header nor data: a banner, a source line, a section label, a footnote. A list of strings; empty if none.
- **description** — one concise sentence: the table's subject, what a single row represents, and the words someone would search for to find it. Use the language of the data itself.

Use the exact row numbers shown, and report the tables top to bottom. You do not mark data rows — the rows between one table's header and the next table's header are that table's data.

Respond ONLY with a JSON object of the form:
{"tables": [{"header_rows": [3], "col_start": 0, "col_end": 11, "title": "...", "notes": [], "description": "..."}]}

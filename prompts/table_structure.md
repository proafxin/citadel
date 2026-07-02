You are given the CONTENTS of one rectangular region of a spreadsheet (a contiguous block of non-empty cells). Identify the logical table(s) it contains and return their structure. Do NOT transcribe or compute any data values — describe structure only.

All row/col offsets are 0-based WITHIN the region (row 0 = the region's first row, col 0 = its first column).

A region may contain: one table; several tables stacked or side-by-side that have DIFFERENT column schemas (return each separately); or one table with a title above and/or notes below.

For each table determine:
- col_start, col_end: its column span, inclusive, region-relative.
- header_rows: the row offsets forming the column header (often just row 0; multiple rows or merged ranges mean a multi-level header). Trust an Excel table object's boundary and the freeze header hint when present.
- data_start, data_end: the first and last data-row offsets (data begins after the header; exclude trailing notes rows).
- columns: one entry PER column from col_start to col_end, in order:
  - header: the column name (resolve a multi-level header into one label; null if blank).
  - dtype: one of string, integer, decimal, float, boolean, date, datetime (infer from the sample values).
  - role: "data" normally, or "section" if the column's values GROUP the rows (a category/section key) rather than being a measurement.
  - unit: a unit implied by the header or values (e.g. %, RM, kg), else null.
- section_label_col: if the table has interspersed SECTION-LABEL ROWS (a row carrying only a group label with the other columns blank), give the column offset where that label sits; otherwise null.
- title: a title for the table (from the text above the region or a spanning merged top row), else null.
- caption, notes: a caption and any footnote/source/notes lines (from text below the region or comments), else null/empty.
- description: a one-paragraph summary of what this table contains (its subject and what the rows/columns represent) that someone searching in natural language for this table would recognize.

Always return a JSON object of the form {"tables": [ ... ]} — a LIST, even when the region holds exactly one table (wrap the single table in the list). Every list field (header_rows, notes, columns) must be an array — use [] when empty, NEVER null. section_label_col, title, and caption are null when absent.

Example output for a region holding one two-column table:
{"tables": [{"col_start": 0, "col_end": 1, "header_rows": [0], "data_start": 1, "data_end": 4, "columns": [{"header": "product", "dtype": "string", "role": "data", "unit": null}, {"header": "price", "dtype": "decimal", "role": "data", "unit": "RM"}], "section_label_col": null, "title": null, "caption": null, "notes": [], "description": "Products and their unit prices in ringgit; one row per product."}]}

Respond ONLY with the JSON object.

Region contents:

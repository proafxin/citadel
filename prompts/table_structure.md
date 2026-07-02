# Table Structure Extractor

You are given the contents of one rectangular region of a spreadsheet (a contiguous block of non-empty cells), together with any surrounding text, merged ranges, and comments. Identify the logical table(s) in the region and describe their structure. Do NOT transcribe or compute any data values — describe structure only.

All row/col offsets are 0-based WITHIN the region (row 0 = the region's first row, col 0 = its first column).

A region may hold one table, several tables stacked or side-by-side that have DIFFERENT columns (return each separately), or a table with a title above and/or notes below.

For each table return:

- col_start, col_end: its column span, inclusive.
- header_rows: the row offsets that form the column header (often just [0]; several rows or merged ranges mean a multi-level header). [] if there is no header row.
- data_start, data_end: the first and last data-row offsets (data begins after the header; exclude title/notes rows).
- title: the table's title if present (from text above the region or a spanning top row), else null.
- caption: a caption if present, else null.
- notes: EVERY other piece of surrounding text as a list of strings — footnotes, source lines, totals/subtotal labels, section notes, stray annotations. Capture anything that is neither a column header nor part of the data, even if it looks like noise. [] if there is none.
- description: a short natural-language summary of what the table contains (its subject and what the rows and columns represent).

Return a JSON object of the form {"tables": [ ... ]} — a LIST, even for a single table. If the region holds no table, return {"tables": []}.

Example:
{"tables": [{"col_start": 0, "col_end": 2, "header_rows": [0], "data_start": 1, "data_end": 8, "title": "Q1 Sales", "caption": null, "notes": ["Figures in RM", "Total: 12,400"], "description": "Quarterly sales by product with quantity and amount."}]}

Region contents:

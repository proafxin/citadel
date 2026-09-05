# Sheet Scan

You are shown a stretch of rows from one worksheet of an excel workbook: every cell that holds something, with the formula behind it where it has one, and how many cells each row holds.

Report every table that begins in these rows. For each, give the row it starts at, the rows that name its columns, the first and last column it occupies, and how its records are laid out.

Tables placed side by side begin at the same row in different columns. Report each of them.

A table's own rows belong to the table, not to a block: the rows naming its columns, its records, the rows that label a section of it, and the rows that total it.

Rows that belong to no table are blocks. Give each block its rows, its kind, and a one-line summary. A block's
kind is one of:

- `title` — names what the worksheet or a part of it is.
- `caption` — names a table or figure beside it.
- `provenance` — says where the contents came from, or when.
- `note` — a remark about the contents.
- `key_value` — a field and its value, as a form is filled in.
- `legend` — says what a mark or abbreviation used elsewhere means.
- `prose` — sentences meant to be read.

How records are laid out is one of:

- `row_records` — each record is a row and the columns are its fields.
- `cross_tab` — the column headers are values of a dimension rather than field names.
- `repeated_groups` — the same set of columns repeats across the sheet, one repeat per entity.
- `matrix` — both axes are dimensions and the cells hold a single measure.

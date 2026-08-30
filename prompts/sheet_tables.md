# Sheet Tables

You are given a text dump of one sheet of an excel workbook, holding every populated cell with the formula behind it where it has one, followed by the sheet's merged ranges, defined names, number formats, hidden rows and bold cells. Tables in a sheet may sit side by side as well as one above another, and a sheet may hold none at all.

Report the tables that are really in this sheet. For each one give the rows and columns it occupies, the rows that name its columns, the rows holding data records, the rows between those that are not data records, its title if it has one, and what each of its columns is called.

Give every row in the sheet a role, and report the regions that are not tables.

## Output

`body_rows` and `row_roles` are lists of spans, each a first and last row.

Every row in the extent must be covered by exactly one `row_roles` span, and those spans must not overlap. Use contiguous spans rather than one per row.

`orientation` is one of:

- `row_records` — each body row is one record and the columns are fields.
- `cross_tab` — the column headers are values of a dimension rather than field names.
- `repeated_groups` — the same set of columns repeats across the sheet, one repeat per entity.
- `matrix` — both axes are dimensions and the cells hold a single measure.

`group_name` names what a table's column groups are instances of, as a column name would read, or is null when its columns are not grouped.

`bands` carries a table's sections: each label with the first and last row it covers. A label may sit on a row of its own or share a row with the header. `band_name` names what those labels are instances of, as a column name would read.

`blocks` carries every region that is not a table, each with its extent and a one-line summary. A sheet with no tables still has blocks.

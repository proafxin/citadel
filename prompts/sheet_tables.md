# Sheet Tables

You are shown the contents of one worksheet from an excel workbook: every cell that holds something, with the
formula behind it where it has one, followed by the worksheet's merged ranges, defined names, number formats,
hidden rows and bold cells. The opening lines say which of the worksheet's rows are shown.

Report the tables that are really there. For each one give the rows and columns it occupies, the rows that name
its columns, the rows holding data records, the rows between those that are not data records, its title if it
has one, and what each of its columns is called.

Give every row shown a role, and report the regions that are not tables.

Report every table at the same level. Do not nest one table inside another, and do not number them as parts of
a larger one. Two blocks that are separate tables are separate, not sections of a common parent.

When a reading is settled, state it once and move on. Do not restate a table you have already described or
reclassify it later in your answer.

## What counts as one table

A table is a set of rows that share one column schema. Where the schema changes, a new table begins.

**Blocks stacked one above another** are one table when they sit under a single header, with each block's
label carried in `bands`. They are separate tables when each block repeats its own header, even when their
columns are identical.

A label naming a group of rows is a value, not a boundary. It belongs in `bands`, not in a table of its own.
The rows under it and the rows under the next label are the same table.

**Blocks placed side by side** are always separate tables. Their columns differ, so their rows cannot be one
record. This holds even when a header block spans across both of them, and even when they occupy the same
rows.

A blank column inside a table is not a boundary when one header names the columns on both sides of it. A blank
column is a boundary when each side has its own title or header.

A region occupying a single populated column is not a table. Columns are the worksheet's own columns;
separators inside a cell's text do not make one.

## Asking for facts

You can ask for measurements over any range instead of guessing. To ask, end your answer with a fenced block
labelled `tools` holding a JSON list. You will be given the results and can then continue or ask again.

- `row_occupancy` — how many cells each row holds. `from_row`, `to_row`, `first_col`, `last_col`.
  Use it when you cannot tell a section label from a record: a label row holds one cell, a record holds many.
- `column_occupancy` — how many cells each column holds, and which are empty. Same arguments.
  Use it to find a blank column separating two groups. A column empty across a table's rows may still hold
  something elsewhere on the worksheet, so ask about the rows you care about.
- `column_values` — the distinct values down a column and how often each occurs. `column`, `from_row`, `to_row`.
  Use it to check whether a column identifies its rows.
- `read_range` — the populated cells in a range. `ref`, such as `A10:G14`.
- `find` — rows matching a regular expression. `pattern`, `from_row`, `to_row`.

```tools
[{"name": "row_occupancy", "arguments": {"from_row": 11, "to_row": 23, "first_col": 1, "last_col": 8}}]
```

## Summary

When you have no more to ask, end your answer with a fenced JSON block giving every region and every block:

```json
{"regions": [{"id": "r1", "rows": [10, 32], "cols": [1, 7],
              "spans": [{"rows": [10, 10], "role": "band_label"},
                        {"rows": [11, 11], "role": "header"},
                        {"rows": [12, 14], "role": "body"}]}],
 "blocks": [{"rows": [1, 3], "cols": [3, 3], "kind": "legend"}]}
```

Every row of a region belongs to exactly one span. Every populated cell belongs to a region or a block. Roles
are `header`, `body`, `totals`, `band_label`, `blank`. Block kinds are `title`, `caption`, `provenance`,
`note`, `key_value`, `legend`, `prose`.

## Output

`body_rows` and `row_roles` are lists of spans, each a first and last row.

Every row shown must be covered by exactly one `row_roles` span, and those spans must not overlap. Use
contiguous spans rather than one per row.

`orientation` is one of:

- `row_records` — each body row is one record and the columns are fields.
- `cross_tab` — the column headers are values of a dimension rather than field names.
- `repeated_groups` — the same set of columns repeats across the sheet, one repeat per entity.
- `matrix` — both axes are dimensions and the cells hold a single measure.

`group_name` names what a table's column groups are instances of, as a column name would read, or is null when
its columns are not grouped.

`bands` carries a table's sections: each label with the first and last row it covers. A label may sit on a row
of its own or share a row with the header. `band_name` names what those labels are instances of, as a column
name would read, and `band_column` is the column the labels sit in when they sit in one of the table's own
columns, or null when they do not.

Where a table is cut off by the rows shown, report it as it appears here. Do not say whether it continues
beyond them — that is known from the rows shown and is not yours to decide.

`blocks` carries every region that is not a table, each with its extent and a one-line summary. Titles,
provenance, notes, legends and form fields belong here. A worksheet with no tables still has blocks.

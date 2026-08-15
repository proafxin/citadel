# Table Structure

## Input

One candidate region from a grid, as its size, its column value-kinds, and its rows:

```
5 rows, 3 cols; column kinds: col0:text, col1:number, col2:number
row 0: Region | Q1 | Q2
row 1: North | 120 | 130
row 2: South | 95 | 101
row 3: East | 80 | 88
row 4: West | 60 | 71
```

Row numbers are the row's real number in the source; some rows in between may be omitted. You never rewrite
or copy out cell values — you return a structural description, and the source rows are applied to it
mechanically afterward.

## Output

A list of tables found in this region. Almost always exactly one entry; empty if this region is not a real
table (a caption, a stray label, a figure's text misread as a grid); more than one only when the region
stacks two or more tables with no gap between them (see "Multiple tables" below). For each table:

| field | type | meaning |
|---|---|---|
| `header_rows` | row numbers | rows that name the columns. Empty if this table has no header row of its own. |
| `transposed` | boolean | true if field names run down a column instead of across a row. |
| `data_start`, `data_end` | row numbers | this table's own data, inclusive. |
| `section_rows` | row numbers | rows that only introduce a group of rows beneath them, within the data range. |
| `columns` | strings | column names, one per column — only when `header_rows` is empty and the columns need names. |
| `title` | string | this table's own title, if one is visibly attached. |
| `notes` | strings | footnotes visibly attached to this table. |

Row numbers always refer to the numbering shown to you, never a re-numbering after transposing.

## Orientation

Most tables list one record per row with field names along the top. A few are turned on their side: field
names run down the first column, one record per following column — set `transposed` true for those. A form
or receipt representing a single record, with field names in one column and their values in the next, is
transposed even though it happens to have only one data column.

## Header row vs. title

A row with a single cell filled, sitting alone above the rest of the table, names the table as a whole, not
a column — put its text in `title` and never in `header_rows`, regardless of which column that cell falls
in. A block of label-and-value pairs (one left column of field names beside exactly one right column of
values) has no header row of its own either: leave `header_rows` empty and, if the columns need names, give
them in `columns` instead. A left column of field-like names followed by TWO OR MORE value columns is a real
comparison table, not a label-value block — it does have a genuine header row.

## Multiple tables

A region can stack more than one table with no gap between them — a metadata strip sitting directly above an
unrelated item table, a summary table tacked onto the end of a data table, each with its own column shape.
Return one entry per table, each covering only its own rows, in top-to-bottom order. Do not confuse this
with a lone label that titles the rows beneath it: if every row under a lone-label row keeps the same shape
as what follows, that label is a title for one table (see above), not a second table.

## Header spans and merging

When a header spans several rows, or a heading covers several columns, list every row that is part of it in
`header_rows` — those rows are combined into one name per column mechanically, so do not write the combined
name yourself.

A cell inside the header block can also state something about the table as a whole rather than naming the
column it happens to sit in — a unit, currency, or scale (`in million USD`, `all figures in %`) is the most
common case. If a header row is only that kind of cell plus otherwise-empty cells, it is not naming any
column: put its text in `notes` and leave that row out of `header_rows` entirely, the same as a title.

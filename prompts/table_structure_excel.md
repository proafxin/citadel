# Table Structure

## Input

One candidate region from a grid, as its size, its column value-kinds, and its lines, each prefixed with its
line number:

N rows, M cols; column kinds: col0:kind, col1:kind, ...
I: cell | cell | cell
J: cell | cell | cell

A line number is that line's real position in the source; some lines in between may be omitted. Never
rewrite or copy out cell values — describe the table using the fields below instead.

This may not be a table. If it is a caption, a stray label, an equation, or other content that only looks
like a grid, return an empty list.

## Output

A list of tables found in this region: one entry per table present, in top-to-bottom order. Empty if nothing
in this region is genuinely tabular data. More than one entry only when the region stacks two or more tables
with no gap between them (see "Multiple tables" below). For each table:

| field | type | meaning |
|---|---|---|
| `header_rows` | line numbers | lines that name the columns. Empty if this table has no header row of its own. |
| `transposed` | boolean | true if field names run down a column instead of across a row. |
| `data_start`, `data_end` | line numbers | this table's own data, inclusive. |
| `section_rows` | line numbers | lines that only introduce a group of rows beneath them, within the data range. |
| `columns` | strings | column names, one per column — only when `header_rows` is empty and the columns need names. |
| `title` | string | this table's own title, if one is visibly attached. |
| `notes` | strings | footnotes visibly attached to this table. |

These fields always refer to the line numbers shown, never a re-numbering after transposing.

When a title, note, or column name is spread across more than one cell, join their text into one clean value
(for example with spaces). Never copy the `|` cell separators from the input into a text value.

## Orientation

`transposed` is true when field names run down a column and each record occupies its own column to the
right — including a single record with one column of values, where each field name becomes its own column
and that value column becomes the data row.

Two columns — one of label-like names, one of values — are transposed only if every row names an attribute
of the same one record. If each row instead names a separate, unrelated record, leave it as an ordinary
two-column table: `transposed` false, `header_rows` empty, no `columns` override needed.

## Header row vs. title

A row with a single cell filled, sitting alone above the rest of the table, names the table as a whole, not
a column: put its text in `title`, never in `header_rows`, regardless of which column that cell falls in. A
left column of field-like names followed by two or more value columns has a genuine header row naming those
columns; a single value column is the transposed case above, not a header row.

## Multiple tables

A region can stack more than one table with no gap between them. Return one entry per table, each covering
only its own rows, in top-to-bottom order. A lone label is a title for one table (see above), not a second
table, when every row beneath it keeps the same shape as what follows.

## Header spans and merging

When a header spans several rows, or a heading covers several columns, list every row that is part of it in
`header_rows`. Do not write the combined column name yourself.

A cell inside the header block that states something about the table as a whole (a unit, currency, or scale)
rather than naming the column it sits in is not naming any column. When a header row is only that kind of
cell plus otherwise-empty cells, put its text in `notes` and leave that row out of `header_rows` entirely,
the same as a title.

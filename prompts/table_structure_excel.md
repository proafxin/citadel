# Table Structure

## Input

One candidate region from a grid, as its size, its column value-kinds, and its lines, each prefixed with its
line number:

N rows, M cols; column kinds: col0:kind, col1:kind, ...
I: cell | cell | cell
J: cell | cell | cell

A line number is that line's real position in the source; some lines in between may be omitted. Never
rewrite or copy out cell values — describe the table using the fields below instead.

## Scope

This region has already been confirmed to hold tabular data before you see it. Your task is to describe its
structure, not to decide whether it qualifies as a table.

Return an empty list only when a specific, checkable condition holds: after setting aside any title line (a
single filled cell alone above the rest, per "Header row vs. title" below) and any footnote lines, zero rows
with values remain. That is the only empty case. A region with two or more rows of values, or one row of
values under a header row, is never empty, regardless of how few rows or columns it has, and regardless of
whether its column names are short, symbolic, or otherwise unusual-looking.

## Output

A list of tables found in this region: one entry per table present, in top-to-bottom order. More than one
entry only when the region stacks two or more tables with no gap between them (see "Multiple tables" below).
For each table:

| field | type | meaning |
|---|---|---|
| `header_rows` | line numbers | lines that name the columns. Empty if this table has no header row of its own. |
| `transposed` | boolean | true if field names run down a column instead of across a row. |
| `data_start`, `data_end` | line numbers | this table's own data, inclusive. |
| `col_start`, `col_end` | column numbers | this table's own columns, inclusive. |
| `section_rows` | line numbers | lines that only introduce a group of rows beneath them, within the data range. |
| `columns` | strings | column names, one per column — only when `header_rows` is empty and the columns need names. |
| `title` | string | this table's own title, if one is visibly attached. |
| `notes` | strings | footnotes visibly attached to this table. |

These fields always refer to the line numbers shown, never a re-numbering after transposing.

When a title, note, or column name is spread across more than one cell, join their text into one clean value
(for example with spaces). Never copy the `|` cell separators, or any other cell's raw text, into a field
that is not itself quoting that cell's own value.

**No line number belongs to more than one of: `title`, a `header_rows` entry, the `data_start`–`data_end`
range, or a footnote.** Each real line in the region has exactly one role. Before answering, check every
line number you are about to place in `header_rows` against `data_start`/`data_end`: if it appears in both,
that is always wrong — decide which single role it actually has and remove it from the other. A header
block never includes the first line of data, however similar that line looks to the header above it.

## Orientation

`transposed` is true when field names run down a column and each record occupies its own column to the
right — including a single record with one column of values, where each field name becomes its own column
and that value column becomes the data row.

To decide whether a two-column region — one column of names, one of values — is transposed, check what the
names column contains:

- If its cells are distinct field names you would expect as column headers for one entity — the kind of
  words that could each label a different attribute of the same thing (a date, an amount, a category, a
  status) — it is transposed. This holds regardless of how many rows there are, including exactly one row
  below a title and exactly two rows with no title at all.
- If its cells instead are different instances of one repeating category — names of people, countries,
  products, or other items where each row is a separate example of the same kind of thing — it is not
  transposed: leave `transposed` false, `header_rows` empty, no `columns` override needed.

## Header row vs. title

A row with a single cell filled, sitting alone above the rest of the table, names the table as a whole, not
a column: put its text in `title`, never in `header_rows`, regardless of which column that cell falls in. A
left column of field-like names followed by two or more value columns has a genuine header row naming those
columns; a single value column is the transposed case above, not a header row.

## Multiple tables

A region can stack more than one table with no gap between them, either one above another or side by side.
Return one entry per table, each covering only its own rows and columns, top-to-bottom then left-to-right
order. A lone label is a title for one table (see above), not a second table, when every row beneath it keeps
the same shape as what follows.

A table's own columns start at its own leftmost column with a value or header in it, even when a blank or
decorative column sits to its left because a different, unrelated table occupies that space in the same
rows. Do not shift `col_start` right of that table's real leftmost column, and do not drop that column's
values into `notes` or `columns` instead of treating it as column 0 of this table.

## Header spans and merging

When a header spans several rows, or a heading covers several columns, list every row that is part of it in
`header_rows`. Do not write the combined column name yourself.

A header block can span rows unevenly: some columns' names may end sooner than others, leaving blank cells
in that row for the remaining columns. Include every such row in `header_rows` regardless, so no row between
the top of the header block and `data_start` is left out of both `header_rows` and the data range. This still
never extends into the first row that holds this table's actual values — see the line-ownership rule above.

A cell inside the header block that states something about the table as a whole (a unit, currency, or scale)
rather than naming the column it sits in is not naming any column. When a header row is only that kind of
cell plus otherwise-empty cells, put its text in `notes` and leave that row out of `header_rows` entirely,
the same as a title.

## Rows that are not data

A row belongs in `data_start`/`data_end` only if it describes one record of its own, the same kind of thing
every other data row describes. A row is excluded instead, the same as a title or header row, when its
identifying cell reads as one of these, regardless of position — above, below, or between the real records:

- an aggregate keyword: total, subtotal, grand total, sum, average, mean, count
- a row labeling the data's source, origin, or a comment about the data collectively, rather than a record
  of it

If a row does not match one of these and instead names or identifies one specific thing the way every other
data row does, it is a data row, even if its values look unusual compared to the rows around it.

# Table Structure

We already know this is exactly one table. Read the grid — shown as its size, its column value-kinds, and
its rows as `row N: cell | cell | ...` — and give back the table itself, correctly structured, as a markdown
table.

## orientation

Most tables list one record per row with field names along the top — write it that way. A few are turned on
their side: field names run down the first column, one record per following column — if so, transpose it so
your markdown table has one row per record instead of mirroring the grid literally. A form or receipt
representing a single record should end up as ONE data row with every field as its own column, not one row
per field.

## structure

Each column is one field, each row one record. Every cell you write is copied verbatim from the grid —
never invent, infer, or alter a value.

- Write ONE header row naming every column, then the data rows. When the source header spans several rows,
  or a heading covers several columns, combine them into one clear name per column — never two columns with
  the same name. A block of label-and-value pairs — one left column of field names beside exactly one right
  column of their values — has no real header of its own: write it with a plain first row anyway, but do not
  invent column names that aren't there. A left column of field-like names followed by TWO OR MORE value
  columns is a real comparison table, not a label-value block — it has a genuine header row like any other
  table.
- Stop before any trailing rows that are not genuine data of this table's own kind.
- A SECTION-LABEL row inside the table — one label, the rest of the row empty, introducing the group of rows
  beneath it — stays in the table as its own row, written with only its first cell filled and the rest
  empty. It is neither header nor data; do not fold it into a neighboring row or drop it.

Before the table, if the table has a title/caption visibly attached to it, write one line: `### title: ...`.
If it has footnotes, write `### notes: ...` (semicolon-separated for more than one). Omit either line
entirely when there is nothing to report. Then the markdown table, and nothing else.

You are given a text dump of one spreadsheet sheet. Identify every table in it and
describe its structure.

# Reading the dump

The grid is inside a fenced block. The first line is a ruler: `#|A(1)|B(2)|C(3)|...`
mapping each position to its column letter and index. Every following line is one row:

    17|TOTAL|||||||||0.0774756738 ‹=SUM(J12:J15)›|542.4812645 ‹=SUM(K12:K15)›

- The text before the first `|` is the sheet row number. Row numbers are the sheet's own,
  and gaps never occur: every row in the extent is present.
- Fields after it are columns in order, starting at the extent's first column. Position N
  in the line is the Nth column in the ruler. Consecutive `|` means empty cells.
- A line with only a row number is a fully blank row.
- Trailing empty cells are omitted; a row ending early means the rest of that row is empty.
- `‹...›` after a value is that cell's formula. The value shown is the cached result.
- Values are as stored: dates ISO, floats at 10 significant digits, `|` escaped as `\|`,
  newlines as `\n`.
- A METADATA section follows the grid: MERGED, TYPES, FORMATS, HIDDEN, FREEZE, AUTOFILTER,
  LISTOBJECTS, DEFINED NAMES, VALIDATION, CONDFMT, COMMENTS, HYPERLINKS, BOLD.
  `-` means none. Read this section before the grid; it is short and carries most of the
  structural evidence.

# Things that are commonly misread

These are cases where the obvious reading is wrong. Everything else about tables you
already know; this list is not a description of what tables look like.

1. An aggregate range BOUNDS the body, it does not equal it. `=SUM(B12:B44)` can sit on a
   sheet whose real body is rows 13-43, because authors pad ranges so later inserts are
   picked up. Use such ranges as an outer bound and to confirm that their own row is a
   totals row. Never take them as the extent.
2. Blank rows are often cosmetic. A sheet can have a blank row between every single data
   row while still being one table. A blank row is not by itself a boundary.
3. A repeated identical header row means a NEW table, not a section break within one.
4. Defined names reflect how the author used the sheet, not its schema, and are also
   frequently oversized for growth or left pointing at deleted cells (`#REF!`). Strong
   evidence, never ground truth.
5. Hidden rows and columns contain real data. Include them and mark them hidden; never
   silently drop them.
6. Cached values of volatile formulas (`=NOW()`, `=TODAY()`, `=RAND()`) are whenever the
   file was last recalculated, not data. Error values (`#REF!`, `#NAME?`, `#N/A`,
   `#VALUE!`) mean the sheet is broken, not that the table is malformed.
7. A sheet may contain no tables at all: key-value forms, legends, notes, packed
   delimited text in one column, or diagonal/triangular computed lattices. Say so rather
   than forcing a rectangle onto them.

# How to work

Account for every row. Do not look only for tables — assign every row in the extent a
role, and the tables will follow. A row you cannot explain is a signal you have missed
something, not a row to skip.

Claim only what you have read. Do not infer a sheet's structure from its size, its column
count, or a summary. If you have not read the rows, read them.

Check anything you derived. Before committing to a table's extent, confirm it against
other evidence in the dump. Prefer evidence that is independent of how you arrived at it.

If a span resists explanation, mark it `unresolved` and say so. Guessing is worse than
reporting a gap; a gap tells the caller where to look.

Stop when every row in the extent has a role and every table extent has been checked.

# Tools

Use these when a claim depends on exact counting or exact column position. They are for
verification, not for reading — the whole sheet is already in front of you. A simple
sheet needs no tool calls at all.

- `find(pattern, from_row, to_row)` — exact occurrence count and matching row numbers.
  Use before asserting that something appears once, or to locate every occurrence.
- `column_values(column, from_row, to_row)` — distinct values and counts in one column.
  Use before asserting a record or entity count.
- `read_range(ref)` — resolve an A1 range to exactly those cells. Use whenever a value's
  column matters and the row has long runs of empty cells, and to check what an aggregate
  range actually covers.

# Output

Return a SheetExtraction. Every row in the extent must appear in `row_roles`. For each
table give the extent, header rows, body rows, totals rows, band-label rows, the column
definitions built by joining stacked header cells top-to-bottom per column, the
orientation, the evidence you used, and a confidence of `corroborated` (an independent
signal agrees), `inferred` (consistent reading, nothing independent), or `uncertain`.

# Table Structure

## Input

One candidate region from a grid, as its size, its column value-kinds, and its lines, each prefixed with its
line number:

N rows, M cols; column kinds: col0:kind, col1:kind, ...
I: cell | cell | cell
J: cell | cell | cell

A line number is that line's real position in the source; some lines in between may be omitted. You never
rewrite or copy out cell values — you return a structural description, and the source lines are applied to
it mechanically afterward.

This region was not identified as a table beforehand — it may turn out to be a caption, a stray label, a
form, an equation, or other content that only happens to look like a grid.

## Output

A list of tables found in this region — one entry per table present. Empty if nothing in this region is
genuinely tabular data. Otherwise almost always exactly one entry; more than one only when the region stacks
two or more tables with no gap between them (see "Multiple tables" below). For each table:

| field | type | meaning |
|---|---|---|
| `header_rows` | line numbers | lines that name the columns. Empty if this table has no header row of its own. |
| `transposed` | boolean | true if field names run down a column instead of across a row. |
| `data_start`, `data_end` | line numbers | this table's own data, inclusive. |
| `section_rows` | line numbers | lines that only introduce a group of rows beneath them, within the data range. |
| `columns` | strings | column names, one per column — only when `header_rows` is empty and the columns need names. |
| `title` | string | this table's own title, if one is visibly attached. |
| `notes` | strings | footnotes visibly attached to this table. |

These fields always refer to the line numbers shown to you, never a re-numbering after transposing.

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

## Examples

These use placeholder cell text (`Col1`, `Item1`, `v1`, ...) purely to show shape — never copy this text
into a real answer, and never let it bias what a real column or value should be named.

**Plain table.** Ordinary header-then-data, nothing else attached.

0: Col1 | Col2 | Col3
1: Item1 | v1 | v2
2: Item2 | v3 | v4

`header_rows: [0]`, `transposed: false`, `data_start: 1`, `data_end: 2`, everything else empty.

**Label-value form (transposed).** One record, field names down the first column, values in the second —
transposed even with a single value column.

0: Field1 | v1
1: Field2 | v2
2: Field3 | v3

`header_rows: []`, `transposed: true`, `data_start: 0`, `data_end: 2`, `columns: []` (field names come from
the first column itself once transposed).

**Title, header, and a scale note.** A lone label above the table is the title, not a header row; a header
row that only states a unit/scale is a note, not a header row.

0: Table1
1: (figures in thousands)
2: Col1 | Col2
3: Item1 | v1
4: Item2 | v2

`title: "Table1"`, `notes: ["(figures in thousands)"]`, `header_rows: [2]`, `data_start: 3`, `data_end: 4`.
Lines 0 and 1 are excluded from both `header_rows` and the data range.

**Spanning header with section rows.** A heading over several columns, plus row(s) that only introduce a
group of the rows beneath them.

0: | GroupA | GroupA | GroupB
1: Col1 | Col2 | Col3 | Col4
2: Section1
3: Item1 | v1 | v2 | v3
4: Item2 | v4 | v5 | v6
5: Section2
6: Item3 | v7 | v8 | v9

`header_rows: [0, 1]` (combined mechanically into one name per column), `data_start: 2`, `data_end: 6`,
`section_rows: [2, 5]`.

**Multiple tables stacked with no gap.** A short metadata strip sitting directly above an unrelated table,
each with its own shape — two entries, not one.

0: Key1 | v1
1: Key2 | v2
2: Col1 | Col2
3: Item1 | v3
4: Item2 | v4

Entry one: `header_rows: []`, `transposed: true`, `data_start: 0`, `data_end: 1`. Entry two: `header_rows:
[2]`, `transposed: false`, `data_start: 3`, `data_end: 4`.

**Not a table.** A caption or stray label with no rows of data beneath it in this region.

0: See appendix for full breakdown.

Empty list — nothing here is tabular data.

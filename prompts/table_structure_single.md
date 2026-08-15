# Table Structure

You are shown one candidate region from a grid — its size, its column value-kinds, and a set of its rows as
`row N: cell | cell | ...`, labelled by their real row number in the source (some rows in between may not be
shown). You never rewrite or copy out the data. You return only a structural description of what is here, as
fields, and the source rows are applied to that description mechanically afterward.

Return a list of tables found in this region:

- **Not a table at all** (a caption, a stray label, a figure's text misread as a grid): return an empty list.
- **One table** (almost always the case): return a list with exactly one entry.
- **More than one table stacked with no gap between them** (a metadata strip sitting directly above an
  unrelated item table, a summary table tacked onto the end of a data table, each with its own column shape):
  return one entry per table, each covering only its own rows, in top-to-bottom order. Do not confuse this
  with a lone label that titles the rows beneath it — if every row under a lone-label row keeps the same
  shape as what follows, that label is a title for one table, not a second table of its own.

For each table entry:

## orientation

Most tables list one record per row with field names along the top. A few are turned on their side: field
names run down the first column, one record per following column. Set `transposed` to true for those — a form
or receipt representing a single record, with field names in one column and their values in the next, is
transposed even though it happens to have only one data column. Row numbers you give (below) always refer to
the row numbering already shown to you, never to a re-numbering after any transform.

## structure

- `header_rows`: the row number(s) that name the columns. When the header spans several rows, or a heading
  covers several columns, list every row that is part of it — those rows get combined into one name per
  column mechanically, you do not need to write the combined name yourself. A block of label-and-value pairs
  (one left column of field names beside exactly one right column of values) has no real header row of its
  own: leave `header_rows` empty rather than pointing at the first data row. A left column of field-like names
  followed by TWO OR MORE value columns is a real comparison table, not a label-value block — it does have a
  genuine header row.
- `data_start` / `data_end`: the row numbers this table's own data runs from and to, inclusive. Stop before
  any trailing rows that are not genuine data of this table's own kind, and before any row that belongs to a
  different stacked table (see above).
- `section_rows`: any row number where one label fills only the first cell, with the rest of the row empty,
  introducing the group of rows beneath it. List these separately — they are neither header nor ordinary data
  and must not be folded into a neighboring row.
- `columns`: leave empty in almost every case — column names are derived mechanically from `header_rows`.
  Only fill this in when the table is genuinely headerless (a label-value block, or a table with no header row
  at all) and the columns need a name; give one short name per column, in order.
- `title` / `notes`: only if a title/caption or footnote is visibly attached to this table in the source; leave
  empty otherwise.

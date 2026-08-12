# Table Structure

We pulled several regions out of one document that we think might be tables — we are not sure. Some may turn
out to be a heading, a caption, or plain running text that only looks tabular. Each region is called a
**block**, numbered `0`, `1`, and so on in the order it appears. Each block is shown as its size, its column
value-kinds, and selected rows as `row N: cell | cell | ...` where N is the row number **within that block**
— you see the headers and unusual rows in full plus a sample of ordinary rows. For each real table you find
among these blocks, give back the table itself, correctly structured, as a markdown table.

Every row belongs to exactly one table — never two. Which table a row belongs to is decided by its columns:
rows with the same columns are the same table; rows with different columns are different tables. The same
rows are never reported twice, not even to show a title separately from the rest — a title is metadata on
the one table, not a table of its own.

1. **Decide which blocks are real tables.** A real table records data: rows of values under columns that
   name what each value is. A block that is a page banner, a heading, a caption, or a line of running text
   dressed as columns is NOT a table — leave it out of your response entirely.
2. **Group blocks that are one table split apart.** Splitting the document sometimes cuts one table into
   several blocks: it continues onto the next page or region, or a blank row breaks it. Signs: the blocks
   have the SAME columns in the same order, and read on naturally. A continuation often has NO header of its
   own (its columns look generic), but the same shape as the block above it — that continuation is the same
   table; write it as one combined markdown table covering both blocks' rows. Blocks with DIFFERENT columns
   are DIFFERENT tables.
3. **Split a block that holds more than one table.** One block can also stack two or more tables by ROW in
   the same region — a metadata strip sitting directly above an unrelated item table, or a summary table
   tacked onto the end of a data table. If a row partway through the block names DIFFERENT columns than the
   rows above it, or opens a fresh label-value section, that row starts a new table, not more rows of the one
   above it. Report each as its own markdown table.

   **Do not confuse this with a lone-label title.** If every row beneath a lone-label row keeps the SAME
   shape it sits atop — same column count, no header row of its own — that label is a title for all of those
   rows together, ONE table (see the title rule below), not a second one-row table. If you are unsure whether
   a lone label starts a new table or titles the one below it, check whether the rows below it change shape
   — no shape change means it is a title, full stop.

## orientation

Most tables list one record per row with field names along the top — write it that way. A few are turned on
their side: field names run down the first column, one record per following column — if so, transpose it so
your markdown table has one row per record instead of mirroring the block literally.

## structure

Each column is one field, each row one record. Every cell you write is copied verbatim from the source —
never invent, infer, or alter a value.

- Write ONE header row naming every column, then the data rows. A header labels columns; it is not data. A
  row of values (even a total or top line) is data, never a header. A block of label-and-value pairs — one
  left column of field names beside exactly one right column of their values — has no real header of its
  own: write it with a plain first row anyway, but do not invent column names. This carve-out is for that
  strict 2-column shape ONLY: a left column of field-like names ("Main read pattern", "Dataset size", ...)
  followed by TWO OR MORE value columns (e.g. one per system being compared) is a real comparison table, not
  a label-value block — it has a genuine header row like any other table. When a source header spans more
  than one row — a top band grouping columns, a row of narrower sub-labels beneath it, sometimes a further
  row of symbols or units — combine ALL of those rows into your one written header row, not just the first: a
  row is still part of the header as long as its cells keep NAMING columns, even if the row above it already
  named the broader group they belong to. A row of numbers stepping in a steady progression across the
  columns (ages, years, bins, ranks, ...) is very likely naming a dimension rather than recording one, even
  though every cell in it is a plain number that would otherwise read as data — weigh it against the rest of
  the block's shape and treat it as header when it fits that pattern; a genuine data row can occasionally
  step evenly too, so confirm from context before excluding it.
- Column names: when the header spans several rows, or a heading covers several columns and sits only in the
  first, combine them into one clear name per column. A top-level label spanning several columns is shared
  by all of them, so combine it with EACH of its own sub-labels to name every one of those columns
  individually (a "Ship Mode: First Class" heading over "Consumer / Corporate / Home Office" sub-columns
  needs three different names — "First Class Consumer", "First Class Corporate", "First Class Home Office" —
  not one name repeated or left blank for the rest). This still applies when a header spans MORE than two
  rows and one of those rows is blank for some columns: a column's name still comes from combining whichever
  of its own header rows actually hold text, never a generic placeholder invented because one row was blank
  there. The same combining applies when a group label covers a paired name-and-value shape rather than
  named sub-columns — one column holding field names, the very next holding their values, with nothing of
  its own to name that value column: combine the group label into it too ("Call Parameters" next to its
  value column becomes "Call Parameters Value"). Never two columns with the same name. A combined name is
  always plain, single-line text — join any line break carried in a cell's own text with a space.
- Stop before any trailing rows that are not genuine data of this table's own kind — an abrupt label, or a
  row whose cells are all the same value where real data would vary across columns.
- A SECTION-LABEL row inside the table — one label, the rest of the row empty, introducing the group of rows
  beneath it, sharing this table's columns — stays in the table as its own row, written with only its first
  cell filled and the rest empty. It is neither header nor data; do not fold it into a neighboring row or
  drop it. A label introducing DIFFERENT columns is a separate table (see point 3), not a section row.

## response format

For each real table, write:

```
### blocks: 0
### title: optional title, omit the line if none
### notes: optional footnotes, semicolon-separated; omit the line if none
| Region | Q1 | Q2 | Q3 |
|---|---|---|---|
| ... | ... | ... | ... |
```

`blocks` lists every block number that makes up this table, in the order they appear, comma-separated (e.g.
`### blocks: 2, 3` for one table split across two blocks). Report tables top to bottom, one after another,
nothing else in your response.

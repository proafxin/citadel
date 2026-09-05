# Sheet Structure

You are shown the contents of one worksheet from an excel workbook, followed by an analysis of it.

Turn the analysis into a `SheetStructure`. Do not re-decide what the analysis concluded; read the worksheet
only to get rows, columns and names right. Where the analysis is vague about a boundary, take the narrowest
reading the worksheet supports.

A `Region` is one table. Give the rows and columns it occupies as numbers, a `row_spans` entry for every row
inside it, and `key_column` — the column whose values say what each row is. Every row between `first_row` and
`last_row` belongs to exactly one span, and spans do not overlap.

Give a `ColumnDef` only for columns the worksheet actually names. Leave a column out rather than numbering it
or inventing a name for it — its position is already known from the region's own columns. Give `group` and
`header_parts` only where they carry something.

Row roles are:

- `header` — the cells name the columns.
- `body` — the row is a record.
- `totals` — the row aggregates records above it.
- `band_label` — the row carries a section label rather than a record.
- `blank` — the row holds nothing.

`bands` carries the sections a region is divided into, each label with the first and last row it covers.
`band_name` names what those labels are instances of, as a column name would read, and `band_column` is the
column the labels sit in when they sit in one of the region's own columns.

`orientation` is one of `row_records`, `cross_tab`, `repeated_groups` or `matrix`. `group_name` names what a
region's column groups are instances of, or is null when its columns are not grouped.

`metadata` carries the rows that belong to no region. Each entry gives its own rows and columns, a one-line
summary, and `region_ids` naming the regions it belongs to. Use an empty list when it belongs to none. A
worksheet with no regions still has metadata. Its `kind` is one of:

- `title` — names what the worksheet or a part of it is.
- `caption` — names a table or figure beside it.
- `provenance` — says where the contents came from, or when.
- `note` — a remark about the contents.
- `key_value` — a field and its value, as a form is filled in.
- `legend` — says what a mark or abbreviation used elsewhere means.
- `prose` — sentences meant to be read.

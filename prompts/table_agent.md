# Worksheet Tables

You are reading one worksheet of an excel workbook, cell by cell. Cells are given as `r<row>: c<col>=<value>`,
listing only the cells that hold something. A trailing `*` on a cell means it is bold. Rows and columns carry
their real sheet numbers, so gaps in the numbering are empty rows and columns.

Report every table in this sheet. Take one action at a time.

- `emit` with a `table` to report one.
- `dismiss` with a `range` `[top, left, bottom, right]` to say those cells belong to no table.
- `amend` with the `index` of a table you already reported and its replacement `table`.
- `read` with a `range` to see any part of the sheet in full.
- `rows_where` with a `column` and what it `holds` — `nothing`, `text`, `number`, `date` or `formula` — to get
  the rows where that is true of that column.
- `outline` to see the sheet as blocks of consecutive populated rows, with the first row of each.
- `done` when nothing is left.

Give only the fields your action needs. When the sheet is shown in full above, you can report its tables
straight away.

A table is reported by position only. Give the rows and columns it occupies, which of its rows name the cells
below or above them, which of its columns name the cells beside them, which rows inside it are labels rather
than records, which row carries its title, and which rows are notes. Leave a list empty when it does not apply,
and use null for a title the sheet does not have.

Every cell that holds something must end up inside a table you emit or inside a range you dismiss.

# Sheet Scan

You are shown a stretch of rows from one worksheet of an excel workbook: every cell that holds something, with
the formula behind it where it has one, and how many cells each row holds.

Report every table that begins in these rows. A table is a set of rows that share one column schema. For each,
give the row it starts at, the rows that name its columns, and the first and last column it occupies.

Tables placed side by side begin at the same row in different columns. Report each of them.

A table's own rows belong to the table, not to a block: the rows naming its columns, its records, the rows
that label a section of it, and the rows that total it.

Everything else is a block — titles, captions, provenance, notes, form fields, prose. Give each block its
rows, its kind, and a one-line summary.

# Sheet Reconciliation

You are given the tables extracted from one sheet of an excel workbook, listed top to bottom by where they sit
in the sheet. Each was extracted from its own region, independently of the others, so a single table may have
been split into several, and a table's column names may sit in a different one than its data.

Each is shown by its index, the rows and columns it occupies in the sheet, its row count, its column names and
value-kinds, its title if it has one, and a few of its rows.

Report the tables that are really in this sheet. For each one give the indices that belong to it in the order
they should be read, the index whose columns name it, and its title if it has one. An index that is not part
of any real table is left out.

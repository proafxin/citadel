# Table Structure

You are given selected rows from a single spreadsheet. Each row is shown as `row N: cell | cell | ...` where N is its row number. You are NOT shown every row — you see the headers and unusual rows in full, plus a sample of the ordinary data rows, enough to understand the shape. Your job is to recover the table's canonical, queryable structure: its columns and its layout. You never return data values beyond what identifies the structure.

A sheet may contain one table or several stacked one below another, sometimes with a title, blank rows, or section labels between them. Find the tables. For each table decide its **layout**:

## relational

An ordinary table: each column is one field, each data row is one record. Report:

- **layout**: "relational".
- **col_start**, **col_end** — first and last column index (0-based, inclusive).
- **header_rows** — the row number(s) whose cells NAME the columns. A header labels the columns; it is not data. A row of values — even an important one like a total or the top line — is data. A row holding one piece of text with the rest empty (a title, a banner, a section label like "Assets") is NOT a header row — put a title in `title`, other such text in `notes`. Some blocks have NO row that names the columns: a list of label/value pairs — a parameters block like `S | 60`, `K | 70`, or `st | 0.11`, often sitting under just a title. There the left column holds field names and the right holds their values; neither row nor column is a header. Return `header_rows` empty for such a block, leave `columns` empty so the columns take plain names, and NEVER promote a label-beside-a-value row to a header.
- **columns** — the resolved name of every column from col_start to col_end, in order. This is the important part: when the header spans several rows, or a group heading covers several columns but sits only in the first (its neighbours blank), combine them into one clear name per column. A heading "First Class" above three columns whose sub-headers are "Consumer", "Corporate", "Home Office" gives "First Class Consumer", "First Class Corporate", "First Class Home Office". Give a short, unambiguous name per column; never leave two columns with the same name.

## crosstab

A matrix: one measure spread across many columns labelled by one or more header rows — for example branches down the rows and a grid of columns, one per (year, quarter), each cell holding that branch's headcount for that period. What makes it a matrix is that the labels naming those columns sit in the HEADER ROWS and the same measure repeats across them; a column whose own cells hold repeated category labels (a "Status" column reading Open, Closed, Open) is an ordinary relational field, not a matrix, however categorical it looks. Turn a matrix back into a normal table with one row per value. Report:

- **layout**: "crosstab".
- **header_rows**, **col_start**, **col_end**, **title**, **notes** — as above.
- **columns** — leave empty for a crosstab.
- **crosstab**:
  - **key_columns** — the columns that identify each row and are NOT part of the matrix (the branch and country columns in the example above), each as `{"name": ..., "col": ...}`.
  - **dimensions** — the header rows that label the matrix columns, each as `{"name": ..., "header_row": ...}`. A dimension value that visually spans several columns but sits only in the first still applies to all of them.
  - **value_name** — what a single matrix cell holds (the headcount in the example above).
  - **value_col_start**, **value_col_end** — the FULL span of the matrix: the first and last column that the dimension header rows label. This is every column that is not a key column, from the first labelled one through the last — not only the columns where you happen to see values in the sample. The matrix is sparse: most cells in any one row are blank, because each row carries its value in just one of them. Judge the span from the header rows, which label all of it, never from where values appear.

## Both

Use the exact row and column numbers shown, and report the tables top to bottom. Do not mark data rows — the rows between one table's header and the next are that table's data.

Respond ONLY with a JSON object like:
{"tables": [{"layout": "relational", "header_rows": [0], "col_start": 0, "col_end": 3, "columns": ["Region", "Q1 Sales", "Q2 Sales", "Q3 Sales"], "title": "...", "notes": []}]}
or, for a matrix whose columns are labelled by two header rows (year on the first, quarter on the second) over two key columns:
{"tables": [{"layout": "crosstab", "header_rows": [0,1], "col_start": 0, "col_end": 5, "columns": [], "crosstab": {"key_columns": [{"name": "Branch", "col": 0}, {"name": "Country", "col": 1}], "dimensions": [{"name": "Year", "header_row": 0}, {"name": "Quarter", "header_row": 1}], "value_name": "Headcount", "value_col_start": 2, "value_col_end": 5}, "title": "...", "notes": []}]}

# Table Structure

Each numbered **block** below is a region we pulled from a document because it might be a table — we are not sure. It may turn out to be a form, a heading, a caption, or plain running text. Every block already covers exactly one complete table candidate: nothing here needs merging with another block or splitting into several.

A block is shown as:

```text
N: R rows, C cols; column kinds: col0:kind, col1:kind, ...
[context before: ...]
[context after: ...]
[source marks row(s) ... as header]
row 0: cell | cell | cell
row 1: cell | cell | cell
```

`context before`/`context after` is the text of the block immediately next to this one — not confirmed to belong to it, judge for yourself. `source marks row(s) ... as header` appears only when the document's own format reliably marks a row as a header (e.g. real HTML `<th>`), never a guess — still verify it actually names columns.

For every block that is a real table, report `transposed`, `col_start`/`col_end`, `header_rows`, `row_end`, `columns`, `section_rows`, `title`, `notes`, `blocks` (the single block number). Respond with a JSON object: `{"tables": [...]}`.

## examples

**Ordinary table.**

```text
0: 3 rows, 3 cols; column kinds: col0:string, col1:integer, col2:integer
row 0: Region | Q1 | Q2
row 1: East | 120 | 98
row 2: West | 75 | 110
```

→ `{"blocks": [0], "header_rows": [0], "row_end": 2, "col_start": 0, "col_end": 2, "columns": ["Region", "Q1", "Q2"], "transposed": false, "section_rows": [], "title": "", "notes": []}`

**Header confirmed by the source, still verified.**

```text
1: 3 rows, 2 cols; column kinds: col0:string, col1:string
source marks row(s) 0 as header
row 0: Name | Role
row 1: A. Ford | Engineer
row 2: B. Diaz | Analyst
```

Row 0 genuinely names both columns, so the hint is trusted: `header_rows: [0]`. Had it not (e.g. the marked row held a value like a real record instead), it would be rejected and header decided from the text alone.

**Multi-row header.**

```text
2: 3 rows, 3 cols; column kinds: col0:string, col1:integer, col2:integer
row 0: | Ship Mode: First Class |
row 1: Segment | Consumer | Corporate
row 2: A123 | 300 | 150
```

Row 0 groups, row 1 sub-labels — both name columns: `header_rows: [0, 1]`, `columns: ["Segment", "First Class Consumer", "First Class Corporate"]` (the group label combined into each sub-column's own name, not left blank or repeated).

**A numeric row that is actually a header.**

```text
3: 2 rows, 4 cols; column kinds: col0:string, col1:integer, col2:integer, col3:integer
row 0: Bin | 1990 | 1991 | 1992
row 1: East | 4 | 9 | 12
```

Row 0's `1990, 1991, 1992` step evenly — a year axis, not data, even though every cell is a plain number: `header_rows: [0]`.

**Label-value form (no header at all) — two shapes, same treatment.**

```text
4: 3 rows, 2 cols; column kinds: col0:string, col1:string
row 0: Name | John Smith
row 1: DOB | 1990-01-01
row 2: City | Denver
```

```text
5: 3 rows, 3 cols; column kinds: col0:string, col1:string, col2:string
row 0: Name | : | John Smith
row 1: DOB | : | 1990-01-01
row 2: City | : | Denver
```

Both are one label column and one value per row — the second just has the colon split into its own cell. Either way: `header_rows: []`, `columns: []`. The test is the *shape* (every row is one label paired with its value), not the exact column count.

**Blank form template — a row of column numbers, not data.**

```text
6: 2 rows, 5 cols; column kinds: col0:string, col1:string, col2:string, col3:string, col4:string
row 0: No | Name | Nationality | Date of Birth | ID Number
row 1: 1 | 2 | 3 | 4 | 5
```

Row 1 isn't a record — it's the columns numbered for reference, a convention in blank official forms, not filled-in data. `header_rows: [0]`, `row_end: 0` — no data rows exist; do not report `1, 2, 3, 4, 5` as if it were one.

**Lone label naming everything beneath it (a title, not a header, not a table of its own).**

```text
7: 3 rows, 2 cols; column kinds: col0:string, col1:decimal
row 0: Calculated Values
row 1: st | 0.11
row 2: KB | 69.04
```

Row 0 is alone, the rest of the row empty, and every row beneath keeps the same shape it introduces (more label-value pairs): `title: "Calculated Values"`, `header_rows: []`, `row_end: 2` — row 0 is never a row of the table, but it is also not a separate one-row table split from the rest, since nothing about the following rows' shape changes.

**Trailing content that stops being this table.**

```text
8: 5 rows, 2 cols; column kinds: col0:string, col1:string
row 0: id | name
row 1: 1 | Alpha
row 2: 2 | Beta
row 3: notes: see appendix
row 4: x | x
```

Row 3 breaks the shape (one label, not two data cells); row 4 repeats the same value in every cell where real data would vary. Neither is more of this table's data: `row_end: 2`.

**Title from context, not from the block itself.**

Given `context before: Table 2: Q3 Purchase Orders` and:

```text
9: 2 rows, 3 cols; column kinds: col0:integer, col1:string, col2:integer
row 0: No | Item | Qty
row 1: 1 | Widget | 4
```

Nothing in the block's own rows names it, so the title comes from context: `title: "Q3 Purchase Orders"` — just the name; if the context sentence had more explanation, that part would go in `notes`, not `title`.

**Section labels inside a table.**

```text
10: 6 rows, 2 cols; column kinds: col0:string, col1:integer
row 0: Product | Units
row 1: Beverages
row 2: Cola | 40
row 3: Juice | 25
row 4: Snacks
row 5: Chips | 60
```

Rows 1 and 4 each carry one label, the rest empty, introducing the rows beneath them, sharing the table's own columns: `header_rows: [0]`, `section_rows: [1, 4]`.

**Transposed.**

```text
11: 2 rows, 3 cols; column kinds: col0:string, col1:string, col2:string
row 0: Name | John | Mary
row 1: Age | 34 | 29
```

Field names run down column 0, one record per following column: `transposed: true`.

# Table Structure

We pulled several regions out of one document that we think might be tables — we are not sure. Some may turn out to be a heading, a caption, or plain running text that only looks tabular. Each region is called a **block**, numbered `0`, `1`, and so on in the order it appears, and already corresponds to one complete table candidate — nothing here needs merging with another block or splitting into several. Each block is shown as its size, its column value-kinds, and selected rows as `row N: cell | cell | ...` where N is the row number **within that block** — you see the headers and unusual rows in full plus a sample of ordinary rows. Some blocks also carry the text of the block immediately before and/or after them in the document, labelled `context before:`/`context after:` — that neighboring text is NOT confirmed to belong to this block; judge for yourself whether it actually names or describes this table before using it. You never return data values beyond what identifies the structure.

For each block, decide whether it is a real table. For every block that is: say so, and report whether it is rotated (`transposed`), its schema (the columns that name what each row holds), and its metadata (`title`, `notes`) — the fields below spell out exactly what to give for each.

Every row belongs to exactly one table — never two.

1. **Decide which blocks are real tables.** A real table records data: rows of values under columns that name what each value is. A block that is a page banner, a heading, a caption, or a line of running text dressed as columns is NOT a table — leave it out entirely.
2. **Report each real table's structure.**

For every table you report, give:

- **blocks** — the single block number.
- The structure fields below.

## orientation

Most tables list one record per row with field names along the top. A few are turned on their side: field names run DOWN the first column, one record per following column. If so, set **transposed** true and stop giving header/columns for it. Otherwise **transposed** false.

## structure

Each column is one field, each row one record.

- **col_start**, **col_end** — first/last column index (0-based, inclusive).
- **header_rows** — the row number(s) whose cells NAME the columns. A header labels columns; it is not data. A row of values (even a total or top line) is data. A block of label-and-value pairs — ONE left column of field names beside EXACTLY ONE right column of their values — has NO header: return `header_rows` empty, leave `columns` empty, never promote a label-beside-value row. When a header spans more than one row — a top band grouping columns, a row of narrower sub-labels beneath it, sometimes a further row of symbols or units — include EVERY one of those rows in `header_rows`, not only the first: a row is still a header as long as its cells keep NAMING columns, even if the row above it already named the broader group they belong to.
- **row_end** — the last row (within this block) that belongs to this table; its data runs from just after the last header row through row_end, inclusive. Normally the block's last row.
- **columns** — the resolved name of every column from col_start to col_end. When the header spans several rows, or a heading covers several columns and sits only in the first, combine them into one clear name per column. A top-level label spanning several columns is not one column's name — it is shared by all of them, so combine it with EACH of its own sub-labels to name every one of those columns individually (a "Ship Mode: First Class" heading over "Consumer / Corporate / Home Office" sub-columns needs three different names — "First Class Consumer", "First Class Corporate", "First Class Home Office" — not one name repeated or left blank for the rest). Never two columns with the same name, and never leave a column unnamed because it shares a top-level label with another.
- **section_rows** — row numbers of SECTION-LABEL rows inside this table: a row carrying one label (the rest empty or that label spanned across the row) that introduces the group of rows beneath it and shares this table's columns. List them; they are neither header nor data.
- **title**, **notes** — a title names the whole table; notes are anything else about it (a footnote, a unit, a caveat). Prefer `context before`/`context after` when it genuinely names or describes this table (e.g. "Table 3: Regional Sales" as the block right before it). Otherwise, a block's own first row can still be a lone label naming it (the rest of the row empty) — treat that the same way. If neither applies, leave both empty.

Report the tables top to bottom. `transposed` is `false` and `section_rows`/`columns` are `[]` when they do not apply. Respond ONLY with a JSON object like:
{"tables": [{"blocks": [0], "transposed": false, "header_rows": [0], "row_end": 6, "col_start": 0, "col_end": 3, "columns": ["Region", "Q1", "Q2", "Q3"], "section_rows": [], "title": "", "notes": []}, {"blocks": [1], "transposed": false, "header_rows": [0], "row_end": 5, "col_start": 0, "col_end": 4, "columns": ["No", "Item", "Qty", "Price", "Amount"], "section_rows": [], "title": "Q3 Purchase Orders", "notes": []}]}
Block 1's title came from its `context before` line ("Table 2: Q3 Purchase Orders") rather than anything inside the block itself — nothing in its own rows names the table, so without that context `title` would stay empty.

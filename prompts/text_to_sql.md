You are given a question and a set of tables, each labeled `t0`, `t1`, ... with its columns and row count. Write the read-only SQL SELECT query (or queries) that extract the data needed to answer the question.

A single query MAY join or aggregate across MULTIPLE of these tables — that is expected and important whenever the answer combines data from more than one table (for example, comparing or summing values that live in different tables).

Reference a table by its label (`t0`, `t1`, ...) and each column as `<table>.<column>` (e.g. `t0.c1`, `t2.c0`). Each table's columns are named `c0`, `c1`, ... exactly as listed for it; always qualify a column with its table.

Rules:
- Read-only SELECT only, over the provided tables `t0`, `t1`, ... — no writes, no semicolons, no comments, no other tables.
- Allowed: SELECT / JOIN / WHERE / GROUP BY / HAVING / ORDER BY / LIMIT and aggregates (sum, avg, count, min, max).
- Give every selected value a clear, human-readable alias with `AS`.
- Express the query logic only; do NOT compute or invent any values.

Return one query per distinct thing the question needs computed (often just one). Respond ONLY with a JSON object: {"queries": ["SELECT ...", ...]} — an empty list if no table can contribute.

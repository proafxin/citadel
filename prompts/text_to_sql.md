# SQL Generator

You are given a question and one or more tables — each labeled `t0`, `t1`, ... with its columns, types, row count, and a few sample rows. Write the SQL query or queries whose results answer the question.

Refer to columns by their position label (`c0`, `c1`, ...) and tables by their label (`t0`, `t1`, ...). Read the sample rows to see how values are written — a filter must match the data's own spelling, such as `'Standard Class'` rather than `'standard class'`.

Use the row count to keep each result small. When the question asks for a count, a total, a maximum, or a figure per group, aggregate with `COUNT`/`SUM`/`GROUP BY` rather than selecting raw rows. Select raw rows only when the question genuinely asks for individual records and there are few of them.

Write a query whenever a table holds data the answer needs. Only when no table has anything to do with the question — the question is about prose, not about any of this data — return an empty list.

Also return `tables`: the labels, as numbers, of every table the answer depends on. List a table here whenever it bears on the question, including when you wrote no query for it. When the question ranks or compares across what you were given — which is largest, which has the most, how many in total — every table it ranges over is one the answer depends on, so list them all rather than only the one you believe wins; the comparison has to be shown, not asserted. Its name, its file, its sheet and its row count are handed to you above and are given to the answer as well, so a question answered by those alone — which file something lives in, how many rows a table has, which is largest — needs no query: list the tables and return no queries. Never write a query that spells out values you were already given; only query what lives inside the rows.

## Examples

question: what is the agreement about
tables:
t0 (form.pdf) rows=4
c0: field (string)  e.g. Tenant name, Address, Phone
c1: value (string)  e.g. Masum, KL Eco City, 03-1234
{"queries": [], "tables": []}

question: which of these has the most rows, and how many
tables:
t0 (orders.csv) rows=9150
c0: Order ID (string)  e.g. CA-2011-100293, CA-2011-140886
c1: Sales (decimal)  e.g. 91.056, 69.216
t1 (returns.xlsx (Sheet1)) rows=834
c0: Order ID (string)  e.g. CA-2011-100293, CA-2011-140886
c1: Returned (string)  e.g. Yes, No
t2 (regions.xlsx (Sheet1)) rows=12
c0: Region (string)  e.g. West, East, Central
c1: Manager (string)  e.g. Ana, Ben, Cara
{"queries": [], "tables": [0, 1, 2]}

question: net income of each company
tables:
t0 (acme_financials.pdf) rows=8
c0: item (string)  e.g. Net income, Revenue, Costs
c1: usd_m (integer)  e.g. 120, 900, 780
t1 (globex_financials.pdf) rows=8
c0: line (string)  e.g. Net income, Revenue, Costs
c1: amount (integer)  e.g. 85, 640, 555
{"queries": ["SELECT t0.c1 AS acme_net_income FROM t0 WHERE t0.c0 = 'Net income'", "SELECT t1.c1 AS globex_net_income FROM t1 WHERE t1.c0 = 'Net income'"], "tables": [0, 1]}

question: total spend for each customer
tables:
t0 (customers.csv) rows=3
c0: id (integer)  e.g. 1, 2, 3
c1: name (string)  e.g. Ana, Ben, Cara
t1 (orders.csv) rows=5
c0: buyer (integer)  e.g. 1, 1, 2
c1: spend (integer)  e.g. 40, 60, 90
{"queries": ["SELECT t0.c1 AS customer, SUM(t1.c1) AS total_spend FROM t0 JOIN t1 ON t0.c0 = t1.c0 GROUP BY t0.c1"], "tables": [0, 1]}

question: how many orders are there per shipping mode
tables:
t0 (sales.xlsx) rows=834
c0: Order ID (string)  e.g. CA-2011-100293, CA-2011-140886
c1: Order Date (string)  e.g. 2013-03-14, 2013-09-30
c2: Ship Mode (string)  e.g. Standard Class, First Class, Second Class
c3: Segment (string)  e.g. Consumer, Corporate, Home Office
c4: Sales (decimal)  e.g. 91.056, 69.216
{"queries": ["SELECT t0.c2 AS ship_mode, COUNT(*) AS orders FROM t0 GROUP BY t0.c2"], "tables": [0]}

question: how many standard class consumer orders are there
tables:
t0 (sales.xlsx) rows=834
c0: Order ID (string)  e.g. CA-2011-100293, CA-2011-140886
c2: Ship Mode (string)  e.g. Standard Class, First Class, Second Class
c3: Segment (string)  e.g. Consumer, Corporate, Home Office
{"queries": ["SELECT COUNT(*) AS orders FROM t0 WHERE t0.c2 = 'Standard Class' AND t0.c3 = 'Consumer'"], "tables": [0]}

Respond ONLY with a JSON object of the form {"queries": ["..."], "tables": [0]}.

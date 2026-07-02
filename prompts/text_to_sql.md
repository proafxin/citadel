# SQL Generator

You are given a question and one or more tables — each labeled `t0`, `t1`, ... with its columns, types, row count, and a few sample rows. Write the SQL query or queries whose results provide the data the question asks for.

## Examples

question: total price of all sales
tables:
t0 (sales.csv) rows=500
c0: shop (string)  e.g. North, South, North
c1: price (integer)  e.g. 225, 300, 150
{"queries": ["SELECT SUM(t0.c1) AS total_price FROM t0"]}

question: total spend for each customer
tables:
t0 (customers.csv) rows=3
c0: id (integer)  e.g. 1, 2, 3
c1: name (string)  e.g. Ana, Ben, Cara
t1 (orders.csv) rows=5
c0: buyer (integer)  e.g. 1, 1, 2
c1: spend (integer)  e.g. 40, 60, 90
{"queries": ["SELECT t0.c1 AS customer, SUM(t1.c1) AS total_spend FROM t0 JOIN t1 ON t0.c0 = t1.c0 GROUP BY t0.c1"]}

question: net income of each company
tables:
t0 (acme_financials.pdf) rows=8
c0: item (string)  e.g. Net income, Revenue, Costs
c1: usd_m (integer)  e.g. 120, 900, 780
t1 (globex_financials.pdf) rows=8
c0: line (string)  e.g. Net income, Revenue, Costs
c1: amount (integer)  e.g. 85, 640, 555
{"queries": ["SELECT t0.c1 AS acme_net_income FROM t0 WHERE t0.c0 = 'Net income'", "SELECT t1.c1 AS globex_net_income FROM t1 WHERE t1.c0 = 'Net income'"]}

question: what is the agreement about
tables:
t0 (form.pdf) rows=4
c0: field (string)  e.g. Tenant name, Address, Phone
c1: value (string)  e.g. Masum, KL Eco City, 03-1234
{"queries": []}

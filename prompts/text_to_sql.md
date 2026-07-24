# SQL Generator

You are given a question and the tables whose rows the answer needs values from, each labeled `t0`, `t1`, ... with its columns (`c0`, `c1`, ...), types, row count, and a few sample rows. Return the SQL queries whose results answer the question.

## Examples

question: net income of each company
tables:
t0 (acme_financials.pdf) rows=8
c0: item (string)  e.g. Net income, Revenue, Costs
c1: usd_m (integer)  e.g. 120, 900, 780
t1 (globex_financials.pdf) rows=8
c0: line (string)  e.g. Net income, Revenue, Costs
c1: amount (integer)  e.g. 85, 640, 555
{"queries": ["SELECT t0.c1 AS acme_net_income FROM t0 WHERE t0.c0 = 'Net income'", "SELECT t1.c1 AS globex_net_income FROM t1 WHERE t1.c0 = 'Net income'"]}

question: total spend for each customer
tables:
t0 (customers.csv) rows=3
c0: id (integer)  e.g. 1, 2, 3
c1: name (string)  e.g. Ana, Ben, Cara
t1 (orders.csv) rows=5
c0: buyer (integer)  e.g. 1, 1, 2
c1: spend (integer)  e.g. 40, 60, 90
{"queries": ["SELECT t0.c1 AS customer, SUM(t1.c1) AS total_spend FROM t0 JOIN t1 ON t0.c0 = t1.c0 GROUP BY t0.c1"]}

question: how many orders are there per shipping mode
tables:
t0 (sales.xlsx) rows=834
c0: Order ID (string)  e.g. CA-2011-100293, CA-2011-140886
c1: Order Date (string)  e.g. 2013-03-14, 2013-09-30
c2: Ship Mode (string)  e.g. Standard Class, First Class, Second Class
c3: Segment (string)  e.g. Consumer, Corporate, Home Office
c4: Sales (decimal)  e.g. 91.056, 69.216
{"queries": ["SELECT t0.c2 AS ship_mode, COUNT(*) AS orders FROM t0 GROUP BY t0.c2"]}

question: how many standard class consumer orders are there
tables:
t0 (sales.xlsx) rows=834
c0: Order ID (string)  e.g. CA-2011-100293, CA-2011-140886
c2: Ship Mode (string)  e.g. Standard Class, First Class, Second Class
c3: Segment (string)  e.g. Consumer, Corporate, Home Office
{"queries": ["SELECT COUNT(*) AS standard_class_consumer_orders FROM t0 WHERE t0.c2 = 'Standard Class' AND t0.c3 = 'Consumer'"]}

Respond ONLY with a JSON object of the form {"queries": ["..."]}.

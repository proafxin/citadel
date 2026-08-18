# Batch and Table Relevance

You are given a question and a numbered inventory drawn from documents already known to bear on it. Each item is one of two kinds. A batch is a summary of one consecutive part of a document. A table is shown by its filename, where in that file it sits, its title or caption when it has one, how many rows it holds, and the names of its columns — never any of its values.

That a document as a whole is relevant does not mean every part of it is. List only the batches and tables the answer cannot do without — an item belongs in a list if the answer would be incomplete or wrong without what it holds. An item that merely sits in a relevant document, without itself bearing on the question, belongs in neither.

Respond with two separate lists — `batches` and `tables` — since the two kinds are examined differently afterward: a listed batch is read in full, a listed table is queried for its actual values.

## Examples

inventory:
[0] batch — lease.pdf part 1: introduces the parties, the property address, and the lease term.
[1] batch — lease.pdf part 2: covers rent amount, due date, deposit, and notice required to end the lease.
[2] table — payments.csv rows=4820: columns tenant, month, amount, status

question: what notice does the tenant have to give to end the lease
{"batches": [1], "tables": []}

question: how much has each tenant paid in total
{"batches": [], "tables": [2]}

question: what does the lease say about rent, and how much has actually been paid
{"batches": [1], "tables": [2]}

Respond ONLY with a JSON object: {"batches": [...], "tables": [...]}.

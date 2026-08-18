# Document Relevance

You are given a question and a numbered inventory of documents in a collection. Each item is a filename and a summary of what that document contains.

List only the documents the answer cannot do without. A document belongs in the list if the answer would be incomplete or wrong without what it holds. A document that merely shares words with the question, or is topically adjacent without actually bearing on it, belongs in neither.

Only the documents you list are examined further. A document you leave out contributes nothing to the answer, no matter how plainly its summary describes something related — nobody after you sees this inventory.

## Examples

inventory:
[0] document — lease.pdf: a residential lease between a landlord and a tenant, covering rent, deposit, notice periods, termination, and repairs.
[1] document — handbook.pdf: an employee handbook covering working hours, leave entitlement, expenses, and conduct.
[2] document — payments.csv: a workbook of tenant payment records.

question: what notice does the tenant have to give to end the lease
{"documents": [0]}

question: what is in this collection
{"documents": [0, 1, 2]}

question: how much has each tenant paid in total
{"documents": [2]}

question: what does the lease say about rent, and what has actually been paid
{"documents": [0, 2]}

Respond ONLY with a JSON object: {"documents": [...]}.

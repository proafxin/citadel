You are given a question and a numbered list of tables, each with its description and column headers. Return the indices of the tables whose data could help answer the question. Judge by whether a query over that table would produce relevant values — not by keyword overlap. Return an empty list if none apply.

Respond ONLY with a JSON object: {"relevant": [0, 2, ...]}.

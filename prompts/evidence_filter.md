# Evidence Filter

You are given a question and a numbered list of items. Each item is either a text passage or a table (shown as its name, columns, and sample rows). Select only the items relevant to answering the question.

Judge relevance by whether the item's content could contribute to the answer: a passage that discusses the subject of the question, or a table whose data could compute or support the answer. Reject items about an unrelated topic, even when they share some words with the question.

Return ONLY a JSON object: {"relevant": [<indices>]} listing the relevant items, most relevant first. If none are relevant, return {"relevant": []}.

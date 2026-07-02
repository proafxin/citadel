You are given a question and a numbered list of evidence items. Each item is either a text passage or a table (shown as its description and columns). Select only the items that are relevant to answering the question.

Judge relevance by whether the item's content could contribute to the answer: a passage that discusses the subject of the question, or a table whose data could compute or support the answer. Reject items that are about an unrelated topic, even when they share some words with the question.

Return ONLY a JSON object: {"relevant": [<indices>]} listing the indices of the relevant items, most relevant first. If none of the items are relevant, return {"relevant": []}.

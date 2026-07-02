You are given a question and a numbered list of evidence items that already passed a first relevance pass. Each item is either a text passage or a computed table result (its columns, row count, and a few sample rows). Choose the items actually needed to answer the question and mark each with a priority tier:

- tier 1 = must-have: the item is load-bearing; the answer is wrong or incomplete without it.
- tier 2 = nice-to-have: the item supports or corroborates the answer but the answer stands without it.

Drop items that, on closer reading, are not needed at all — do not list them.

Return ONLY a JSON object: {"selected": [{"id": <index>, "tier": 1 or 2}, ...]}. If nothing is needed, return {"selected": []}.

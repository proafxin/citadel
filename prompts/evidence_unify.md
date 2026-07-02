You are given a question and a numbered list of items. Each item is either a text passage or a table result (shown as its columns, row count, and a few sample rows). Choose the items actually needed to answer the question, and mark each as either essential or supporting.

Leave out any item that, on closer reading, is not needed at all.

Return ONLY a JSON object: {"selected": [{"id": <index>, "tier": 1 or 2}, ...]} where tier 1 = essential (the answer is wrong or incomplete without it) and tier 2 = supporting (adds context but the answer stands without it). If nothing is needed, return {"selected": []}.

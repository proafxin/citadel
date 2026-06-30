This description is the table's RETRIEVAL TEXT — it will be turned into an embedding and matched against users' natural-language search queries (like a search engine). Write it so that questions about this table's subject and contents will hit it.

You are given the table's columns and a few sample rows. Write ONE concise paragraph that:
- states the table's subject/domain, what a single row represents, and what the columns measure or contain;
- surfaces the concrete entities, names, and vocabulary a user would actually search for (use the language of the data itself, not generic phrasing);
- conveys the kinds of questions this table can answer.

Summarize the table's meaning rather than mechanically listing every column, and do not invent anything beyond the columns and sample rows. Respond ONLY with a JSON object: {"description": "..."}.

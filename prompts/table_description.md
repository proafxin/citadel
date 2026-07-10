# Table Descriptor

You are given a table's columns and a few sample rows. Write ONE concise paragraph describing it, so that someone searching in natural language for this table's subject would recognize it as the right table.

The paragraph should:

- state the table's subject, what a single row represents, and what the columns measure or contain;
- surface the concrete entities, names, and vocabulary someone would actually search for (use the language of the data itself, not generic phrasing);
- convey the kinds of questions this table can answer.

You may also be given the calculations used in the table, written as spreadsheet expressions. Use them only to say which columns hold figures computed from other columns and what they compute, since that distinguishes a derived figure from a recorded one. Do not quote the expressions.

Summarize the table's meaning rather than mechanically listing every column, and do not invent anything beyond what you are given. Respond ONLY with a JSON object: {"description": "..."}.

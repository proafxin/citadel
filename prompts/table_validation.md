# Table Validation

You are given candidate tables extracted from one document, each numbered and shown as its column names followed by a few of its rows. Some of these are not real tables: a page header or running banner, a title block, a caption, or other non-tabular text can be captured by mistake and given a table shape.

A real table records data: rows of values under columns that name what each value is. A candidate that is a repeated page banner, a heading, or a single line of running text dressed as columns is not a table.

Return the numbers of the candidates that are real tables, and only those.

Respond ONLY with a JSON object: {"tables": [<number>, ...]}.

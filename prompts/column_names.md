You are given a table's current column headers (some may be blank or generic) and a few sample rows. Return a clear, concise header for EVERY column, in order.

- Keep existing meaningful headers unchanged.
- For a blank or generic header, infer a good name from the sample values in that column and the surrounding headers.
- Use the vocabulary of the data itself; do not invent columns, and do NOT transcribe or compute any data values.

Respond ONLY with a JSON object: {"headers": ["...", ...]} with exactly one entry per column, in the given order.

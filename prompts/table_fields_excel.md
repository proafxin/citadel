# Table structure

You are given a table. The table is represented in the following textual format:

row i: col 0: "A1" | col 1: "B1" | ... | col N: "N1"
row j: col 0: "A2" | col 1: "B2" | ... | col N: "N2"

Each pipe works as a delimiter to separate the individual cells in a row. Examine the table. Identify the data rows semantically and structurally from the context then return the start and end row indices as:
{"start_row": int, "end_row": int}

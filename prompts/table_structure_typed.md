# Table Detection

You are given a region of lines extracted from an excel sheet cell grid. Consider the whole region as one unit, and check contextually whether it contains one or more meaningful tables. Report how many tables there are, and for each one, its bounding box (start row, end row, start column, end column).

Within each table's rows, also label which rows are header rows (name the columns), which are data rows, and which are metadata (a title, caption, note, or aggregation/total row that isn't itself a data record).

Give the final answer as:
{"header_rows": [int, ...], "data_start": int, "data_end": int, "metadata_rows": [int, ...]}

# Table Structure Extractor

You will be given some blocks of lines. In each block, there will be some lines. These blocks were extracted from tabular data which we considered to be candidate tables. Your job is to give us the actual structure of the table schema as well as any metadata. For the table structure, you should specify which line ranges form the actual rows of the table, which line ranges form the header rows and which line ranges form metadata such as title, caption, notes/comments, or any remaining metadata as well as which category the metadata belongs to. The output format should be a json. Note that some blocks may not even be valid tables. So, first decide if the table is valid or not. In the output only return the valid tables keyed by their id. Note that you need to parse each table properly to understand the underlying row column style structure first and there can be many deviations from a standard clean table (for example, a column may span multiple cells and there can be many other deviations which we can't really put cleanly in heuristics or fixed set of exhaustive rules. That's what you have to figure out). The following examples should be used to verify your understanding of the intent rather than treating them as exhaustive set of possibilities. Omit any key from your output whose value would otherwise be empty, null, or not applicable to that table — do not emit empty objects, empty arrays, or null values, just leave the key out entirely. Sometimes a table is in fact a form which we can consider to be a transposed table. This means detecting layout or relation between the data lines of the candidate table is also your job. You should also give us the layout type, which is exactly one of: rectangular, transpose, crosstab. For crosstab, give `key_columns`, `dimensions` (each `{label, line}`), and `value_start`/`value_end` instead of `columns`. If candidates are really one table split apart (only ever adjacent ones), report one entry with a `tables` list and line references as `{table, line}` instead of a bare line number.

## Sample Input

Table 1:
0: some table text,,
1: name, title, description
2: ABC, Some title, Some description
3: DEF, Another title, Another description

Table 2:
0: , Region,,,,
1: Quarter, North, South, East, West, Central
2: Q1, 300, 150, 90, 210, 175
3: Q2, 200, 175, 60, 190, 140
4: Q3, 260, 205, 75, 220, 160
5: Total, 760, 530, 225, 620, 475

Table 3:
0: This report summarizes quarterly
1: performance across all regions for
2: the fiscal year ending in December.

Table 4:
0: Name, John Smith
1: DOB, 1990-01-01
2: City, Denver

Table 5:
0: Bin, 1990, 1991, 1992
1: East, 4, 9, 12
2: West, 6, 3, 15

Table 6:
0: SAMPLE MEASUREMENT LOG,,,,,,,
1: , Method, Alpha, , , Beta, ,
2: , Group, X, Y, Z, X, Y, Z
3: Record ID, Date, , , , , ,
4: R-1001, 2013-03-14, , , , , , 91.06
5: R-1002, 2013-03-15, 61.5, , , , ,

Table 7:
0: id, key_a, key_b, key_c, key_d, key_e, amount, price
1: 1001, 12, 4, , , 1, 13.99, 13.99
2: 1001, 12, 5, 19, , 3, 14.99, 9.99
3: Reference notes:,,,,,,,
4: key_a values:, 12, 12, 12, 15, 8, 8, 8

Table 8:
0: Field, Employee 1, Employee 2
1: Name, Priya Nair, J. Alvarez
2: Department, Logistics, Finance
3: Start Date, 2021-06-01, 2019-11-15

Table 9:
0: Property, System A, System B, System C
1: Read latency (p99), 4ms, 12ms, 3ms
2: Write latency (p99), 9ms, 6ms, 11ms
3: Storage engine, LSM-tree, B-tree, LSM-tree

Table 11:
0: Batch, Yield %, Operator, Notes
1: B-204, 91.2, Kim,
2: B-205, 88.7, Kim,
3: B-206, 94.0, Osei, rerun after calibration

Table 12:
0: B-207, 90.5, Osei,
1: B-208, 92.1, Fischer,
2: B-209, 89.0, Fischer, sensor drift noted

Table 13:
0: SEGMENT SATISFACTION SURVEY,,,,,,,,,
1: , , 2024, , , , 2025, , ,
2: , , Online, , Store, , Online, , Store,
3: Segment, Cohort, Score, N, Score, N, Score, N, Score, N
4: Enterprise, New, 4.2, 118, 4.0, 64, 4.4, 121, 4.1, 70
5: Enterprise, Returning, 4.6, 340, 4.5, 210, 4.7, 355, 4.6, 225
6: SMB, New, 3.9, 88, 3.7, 40, 4.0, 95, 3.8, 47
7: SMB, Returning, 4.3, 260, 4.2, 150, 4.4, 270, 4.3, 160
8: Scores are on a 5-point scale; N is respondent count.,,,,,,,,,

## Sample Output

{
    1: {
        metadata: {title: 0},
        rows_start: 2,
        rows_end: 3,
        headers_start: 1,
        headers_end: 1,
        columns: [
            {label: "name", index: 0},
            {label: "title", index: 1},
            {label: "description", index: 2}
        ],
        layout: "rectangular"
    },
    2: {
        metadata: {total: 5},
        rows_start: 2,
        rows_end: 4,
        headers_start: 0,
        headers_end: 1,
        columns: [
            {label: "Quarter", index: 0},
            {label: "Region North", index: 1},
            {label: "Region South", index: 2},
            {label: "Region East", index: 3},
            {label: "Region West", index: 4},
            {label: "Region Central", index: 5}
        ]
    },
    4: {
        rows_start: 0,
        rows_end: 2
    },
    5: {
        rows_start: 1,
        rows_end: 2,
        headers_start: 0,
        headers_end: 0,
        columns: [
            {label: "Bin", index: 0},
            {label: "1990", index: 1},
            {label: "1991", index: 2},
            {label: "1992", index: 3}
        ]
    },
    6: {
        metadata: {title: 0},
        rows_start: 4,
        rows_end: 5,
        headers_start: 1,
        headers_end: 3,
        columns: [
            {label: "Record ID", index: 0},
            {label: "Date", index: 1},
            {label: "Alpha X", index: 2},
            {label: "Alpha Y", index: 3},
            {label: "Alpha Z", index: 4},
            {label: "Beta X", index: 5},
            {label: "Beta Y", index: 6},
            {label: "Beta Z", index: 7}
        ]
    },
    7: {
        rows_start: 1,
        rows_end: 2,
        headers_start: 0,
        headers_end: 0,
        columns: [
            {label: "id", index: 0},
            {label: "key_a", index: 1},
            {label: "key_b", index: 2},
            {label: "key_c", index: 3},
            {label: "key_d", index: 4},
            {label: "key_e", index: 5},
            {label: "amount", index: 6},
            {label: "price", index: 7}
        ]
    },
    8: {
        rows_start: 1,
        rows_end: 3,
        headers_start: 0,
        headers_end: 0,
        columns: [
            {label: "Field", index: 0},
            {label: "Employee 1", index: 1},
            {label: "Employee 2", index: 2}
        ],
        layout: "transpose"
    },
    9: {
        rows_start: 1,
        rows_end: 3,
        headers_start: 0,
        headers_end: 0,
        columns: [
            {label: "Property", index: 0},
            {label: "System A", index: 1},
            {label: "System B", index: 2},
            {label: "System C", index: 3}
        ]
    },
    "11,12": {
        tables: [11, 12],
        headers_start: {table: 11, line: 0},
        headers_end: {table: 11, line: 0},
        rows_start: {table: 11, line: 1},
        rows_end: {table: 12, line: 2},
        columns: [
            {label: "Batch", index: 0},
            {label: "Yield %", index: 1},
            {label: "Operator", index: 2},
            {label: "Notes", index: 3}
        ]
    },
    13: {
        metadata: {title: 0, notes: 8},
        rows_start: 4,
        rows_end: 7,
        headers_start: 1,
        headers_end: 3,
        key_columns: [
            {label: "Segment", index: 0},
            {label: "Cohort", index: 1}
        ],
        dimensions: [
            {label: "Year", line: 1},
            {label: "Channel", line: 2},
            {label: "Metric", line: 3}
        ],
        value_start: 2,
        value_end: 9,
        layout: "crosstab"
    }
}

Table 2's row 5 is a Total row summing each region's column, not another quarter's record — it's real derived data, not junk, so it's kept as `metadata: {total: 5}` rather than silently dropped, while `rows_end: 4` still keeps it out of the actual data rows. This differs from Table 7 below, where the excluded rows are a bare label and an echoed reference list, not a value worth keeping, so they get no metadata entry at all. Table 3 is not a real table (plain running text broken across lines) and is correctly absent from the output entirely. Table 7's rows 3 and 4 break the shape established by rows 0-2 (a bare label, then a row whose cells are mostly a repeated echo of one column's own values rather than a new record) and are correctly excluded by `rows_end: 2` rather than folded in as more data or reported as metadata. Table 8 is left in its given orientation (row 0 is still literally the header row as shown) rather than pre-transposed — `layout: "transpose"` tells the consumer to flip it afterward, the same way `layout: "rectangular"` needs no flip. Table 9 looks label-value at a glance (column 0 reads like field names) but has two or more real value columns, not one, so its first row is a genuine header naming every column, not a label-value block. Tables 11 and 12 are one table split apart: 12 has no header row of its own, only a continuation of 11's data, so `headers_start`/`headers_end` both point at table 11 while `rows_end` points at table 12's last line. Table 13 is a crosstab with two key columns (Segment, Cohort) and three stacked dimension rows (Year, Channel, Metric) rather than one — each dimension is reported on its own line instead of merging Year+Channel+Metric into a single combined column label.

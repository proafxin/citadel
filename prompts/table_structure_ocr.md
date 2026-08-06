# Table Structure Extractor

You will be given some blocks of lines. In each block, there will be some lines. These blocks were extracted from tabular data which we considered to be candidate tables. Your job is to give us the actual structure of the table schema as well as any metadata. For the table structure, you should specify which line ranges form the actual rows of the table, which line ranges form the header rows and which line ranges form metadata such as title, caption, notes/comments, or any remaining metadata as well as which category the metadata belongs to. The output format should be a json. Note that some blocks may not even be valid tables. So, first decide if the table is valid or not. In the output only return the valid tables keyed by their id. Note that you need to parse each table properly to understand the underlying row column style structure first and there can be many deviations from a standard clean table (for example, a column may span multiple cells and there can be many other deviations which we can't really put cleanly in heuristics or fixed set of exhaustive rules. That's what you have to figure out). The following examples should be used to verify your understanding of the intent rather than treating them as exhaustive set of possibilities. Omit any key from your output whose value would otherwise be empty, null, or not applicable to that table — do not emit empty objects, empty arrays, or null values, just leave the key out entirely. Sometimes a table is in fact a form which we can consider to be a transposed table. This means detecting layout or relation between the data lines of the candidate table is also your job. You should also give us the layout type which can be: rectangular, crosstab, pivot, transpose.

## Sample Input

Table 1:
0: some table text,,
1: name, title, description
2: ABC, Some title, Some description
3: DEF, Another title, Another description

Table 2:
0: Region,,
1: North, South, East
2: 300, 150, 90
3: 200, 175, 60

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

Table 10:
0: QUARTERLY REVIEW,,,
1: North Division,,,
2: Segment, Q1, Q2, Q3
3: Retail, 420, 465, 501
4: Wholesale, 310, 298, 340
5: South Division,,,
6: Segment, Q1, Q2, Q3
7: Retail, 180, 205, 190
8: Wholesale, 90, 88, 102

## Sample Output

{
    1: {
        metadata: {title: 0},
        rows_start: 2,
        rows_end: 3,
        headers_start: 1,
        headers_end: 1,
        columns: [
            {label: "name", start: 0, end: 0},
            {label: "title", start: 1, end: 1},
            {label: "description", start: 2, end: 2}
        ],
        layout: "rectangular"
    },
    2: {
        rows_start: 2,
        rows_end: 3,
        headers_start: 0,
        headers_end: 1,
        columns: [
            {label: "Region North", start: 0, end: 0},
            {label: "Region South", start: 1, end: 1},
            {label: "Region East", start: 2, end: 2}
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
            {label: "Bin", start: 0, end: 0},
            {label: "1990", start: 1, end: 1},
            {label: "1991", start: 2, end: 2},
            {label: "1992", start: 3, end: 3}
        ]
    },
    6: {
        metadata: {title: 0},
        rows_start: 4,
        rows_end: 5,
        headers_start: 1,
        headers_end: 3,
        columns: [
            {label: "Record ID", start: 0, end: 0},
            {label: "Date", start: 1, end: 1},
            {label: "Alpha X", start: 2, end: 2},
            {label: "Alpha Y", start: 3, end: 3},
            {label: "Alpha Z", start: 4, end: 4},
            {label: "Beta X", start: 5, end: 5},
            {label: "Beta Y", start: 6, end: 6},
            {label: "Beta Z", start: 7, end: 7}
        ]
    },
    7: {
        rows_start: 1,
        rows_end: 2,
        headers_start: 0,
        headers_end: 0,
        columns: [
            {label: "id", start: 0, end: 0},
            {label: "key_a", start: 1, end: 1},
            {label: "key_b", start: 2, end: 2},
            {label: "key_c", start: 3, end: 3},
            {label: "key_d", start: 4, end: 4},
            {label: "key_e", start: 5, end: 5},
            {label: "amount", start: 6, end: 6},
            {label: "price", start: 7, end: 7}
        ]
    },
    8: {
        rows_start: 1,
        rows_end: 3,
        headers_start: 0,
        headers_end: 0,
        columns: [
            {label: "Field", start: 0, end: 0},
            {label: "Employee 1", start: 1, end: 1},
            {label: "Employee 2", start: 2, end: 2}
        ],
        layout: "transpose"
    },
    9: {
        rows_start: 1,
        rows_end: 3,
        headers_start: 0,
        headers_end: 0,
        columns: [
            {label: "Property", start: 0, end: 0},
            {label: "System A", start: 1, end: 1},
            {label: "System B", start: 2, end: 2},
            {label: "System C", start: 3, end: 3}
        ]
    }
}

Table 3 is not a real table (plain running text broken across lines) and is correctly absent from the output entirely. Table 7's rows 3 and 4 break the shape established by rows 0-2 (a bare label, then a row whose cells are mostly a repeated echo of one column's own values rather than a new record) and are correctly excluded by `rows_end: 2` rather than folded in as more data or reported as metadata. Table 8 is left in its given orientation (row 0 is still literally the header row as shown) rather than pre-transposed — `layout: "transpose"` tells the consumer to flip it afterward, the same way `layout: "rectangular"` needs no flip. Table 9 looks label-value at a glance (column 0 reads like field names) but has two or more real value columns, not one, so its first row is a genuine header naming every column, not a label-value block.

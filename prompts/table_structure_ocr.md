# Table Structure Extractor

You will be given some blocks of lines. In each block, there will be some lines. These blocks were extracted from tabular data which we considered to be candidate tables. Your job is to give us the actual structure of the table schema as well as any metadata. For the table structure, you should specify which line ranges form the actual rows of the table, which line ranges form the header rows and which line ranges form metadata such as title, caption, notes/comments, or any remaining metadata as well as which category the metadata belongs to. The output format should be a json. Note that some blocks may not even be valid tables. So, first decide if the table is valid or not. In the output only return the valid tables keyed by their id. Note that you need to parse each table properly to understand the underlying row column style structure first and there can be many deviations from a standard clean table (for example, a column may span multiple cells and there can be many other deviations which we can't really put cleanly in heuristics or fixed set of exhaustive rules. That's what you have to figure out). The following examples should be used to verify your understanding of the intent rather than treating them as exhaustive set of possibilities.

## Sample Input

Table 1:
0: some table text | |
1: name | title | description
2: ABC | Some title | Some description
3: DEF | Another title | Another description

Table 2:
0: | Region | Region | Region | Region | Region
1: Quarter | North | South | East | West | Central
2: Q1 | 300 | 150 | 90 | 210 | 175
3: Q2 | 200 | 175 | 60 | 190 | 140
4: Q3 | 260 | 205 | 75 | 220 | 160
5: Total | 760 | 530 | 225 | 620 | 475

Table 3:
0: This report summarizes quarterly
1: performance across all regions for
2: the fiscal year ending in December.

Table 4:
0: Name | John Smith
1: DOB | 1990-01-01
2: City | Denver

Table 5:
0: Reference | : | REF-48213
1: Status | : |
2: Description | : | Replacement part, second shipment
3: Contact | : | 555-0142

Table 6:
0: Bin | 1990 | 1991 | 1992
1: East | 4 | 9 | 12
2: West | 6 | 3 | 15

Table 7:
0: Group | System A | System A | System B | System B | System C | System C | System D | System D
1: Group | Avg | Std | Avg | Std | Avg | Std | Avg | Std
2: control | 4.2 | 0.3 | 3.9 | 0.2 | 4.1 | 0.4 | 3.7 | 0.5
3: treatment | 4.6 | 0.2 | 4.3 | 0.3 | 4.5 | 0.2 | 4.0 | 0.4

Table 8:
0: id | key_a | key_b | key_c | key_d | key_e | amount | price
1: 1001 | 12 | 4 | | | 1 | 13.99 | 13.99
2: 1001 | 12 | 5 | 19 | | 3 | 14.99 | 9.99
3: Reference notes: | | | | | | |
4: key_a values: | 12 | 12 | 12 | 15 | 8 | 8 | 8

Table 9:
0: Field | Employee 1 | Employee 2
1: Name | Priya Nair | J. Alvarez
2: Department | Logistics | Finance
3: Start Date | 2021-06-01 | 2019-11-15

Table 10:
0: Property | Model X | Model Y | Model Z
1: Weight (kg) | 4.2 | 3.8 | 5.1
2: Power draw (W) | 60 | 45 | 75
3: Housing material | Aluminum | Steel | Aluminum

Table 11:
0: CODE | DESCRIPTION
1: Category A |
2: 101 | Widget assembly
3: 102 | Widget inspection
4: 103 | Widget packaging
5: Category B |
6: 201 | Gadget assembly
7: 202 | Gadget testing

Table 12:
0: Ref | Description | Count | Rate | Line Total
1: 1 | Widget assembly kit | 2 | 25.00 | 50.00
2: 2 | Gadget mounting bracket | 1 | 45.00 | 45.00
3: Total | | | | 95.00

Table 13:
0: Batch | Yield % | Operator | Notes
1: B-204 | 91.2 | Kim |
2: B-205 | 88.7 | Kim |
3: B-206 | 94.0 | Osei | rerun after calibration

Table 14:
0: B-207 | 90.5 | Osei |
1: B-208 | 92.1 | Fischer |
2: B-209 | 89.0 | Fischer | sensor drift noted

Table 15:
0: QUARTERLY MAINTENANCE LOG

Table 16:
0: Equipment | Last Serviced | Next Due
1: Compressor Unit 2 | 2026-01-14 | 2026-07-14
2: Backup Generator | 2025-11-02 | 2026-05-02

Table 17:
0: Fire Suppression Panel | 2026-02-20 | 2026-08-20
1: Water Pump Station | 2025-12-30 | 2026-06-30

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
        ]
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
        rows_end: 2,
        layout: "transpose"
    },
    5: {
        rows_start: 0,
        rows_end: 3,
        layout: "transpose"
    },
    6: {
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
    7: {
        rows_start: 2,
        rows_end: 3,
        headers_start: 0,
        headers_end: 1,
        columns: [
            {label: "Group", index: 0},
            {label: "System A Avg", index: 1},
            {label: "System A Std", index: 2},
            {label: "System B Avg", index: 3},
            {label: "System B Std", index: 4},
            {label: "System C Avg", index: 5},
            {label: "System C Std", index: 6},
            {label: "System D Avg", index: 7},
            {label: "System D Std", index: 8}
        ]
    },
    8: {
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
    9: {
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
    10: {
        rows_start: 1,
        rows_end: 3,
        headers_start: 0,
        headers_end: 0,
        columns: [
            {label: "Property", index: 0},
            {label: "Model X", index: 1},
            {label: "Model Y", index: 2},
            {label: "Model Z", index: 3}
        ]
    },
    11: {
        rows_start: 1,
        rows_end: 7,
        headers_start: 0,
        headers_end: 0,
        columns: [
            {label: "CODE", index: 0},
            {label: "DESCRIPTION", index: 1}
        ],
        sections: [
            {label: "Category A", line: 1},
            {label: "Category B", line: 5}
        ]
    },
    12: {
        metadata: {total: 3},
        rows_start: 1,
        rows_end: 2,
        headers_start: 0,
        headers_end: 0,
        columns: [
            {label: "Ref", index: 0},
            {label: "Description", index: 1},
            {label: "Count", index: 2},
            {label: "Rate", index: 3},
            {label: "Line Total", index: 4}
        ]
    },
    "13,14": {
        tables: [13, 14],
        headers_start: {table: 13, line: 0},
        headers_end: {table: 13, line: 0},
        rows_start: {table: 13, line: 1},
        rows_end: {table: 14, line: 2},
        columns: [
            {label: "Batch", index: 0},
            {label: "Yield %", index: 1},
            {label: "Operator", index: 2},
            {label: "Notes", index: 3}
        ]
    },
    "16,17": {
        metadata: {title: {table: 15, line: 0}},
        tables: [16, 17],
        headers_start: {table: 16, line: 0},
        headers_end: {table: 16, line: 0},
        rows_start: {table: 16, line: 1},
        rows_end: {table: 17, line: 1},
        columns: [
            {label: "Equipment", index: 0},
            {label: "Last Serviced", index: 1},
            {label: "Next Due", index: 2}
        ]
    }
}

Table 2's row 5 is a Total row summing each region's column, not another quarter's record — it's real derived data, not junk, so it's kept as `metadata: {total: 5}` rather than silently dropped, while `rows_end: 4` still keeps it out of the actual data rows. Table 2's row 0 shows "Region" repeated 5 times (it spans only the 5 region columns, not the whole line), while Table 1's row 0 shows a single title spanning the entire line and appears once with the rest blank — that's the one case where a span is blanked instead of repeated. Table 3 is not a real table (plain running text broken across lines) and is correctly absent from the output entirely. Table 5 is a label/value form with a literal separator column in the middle (the colon) — still just two logical fields per row, no header, same treatment as Table 4. Table 7's row 1 repeats "Group" from row 0 at position 0 — that's the same single value carrying down from directly above, not a second distinct label, so the combined column stays "Group", not "Group Group"; the other columns combine their group name with their own sub-label ("System A Avg") the same way Table 2 combines "Region" with each region name. Table 8's rows 3 and 4 break the shape established by rows 0-2 (a bare label, then a row whose cells are mostly a repeated echo of one column's own values rather than a new record) and are correctly excluded by `rows_end: 2` rather than folded in as more data or reported as metadata. Table 9 is left in its given orientation (row 0 is still literally the header row as shown) rather than pre-transposed — `layout: "transpose"` tells the consumer to flip it afterward. Table 10 looks label-value at a glance (column 0 reads like field names) but has two or more real value columns, not one, so its first row is a genuine header naming every column, not a label-value block. Table 11's rows 1 and 5 are section labels, not data or metadata — each introduces a run of following rows that still share the table's own two columns, so they're reported as `sections` rather than as their own table or a title. Table 12's row 3 is a Total row, kept the same way as Table 2's. Tables 13 and 14 are one table split apart: 14 has no header row of its own, only a continuation of 13's data, so `headers_start`/`headers_end` both point at table 13 while `rows_end` points at table 14's last line. Table 15 is a lone title line that landed in its own candidate — not a table on its own, and not one of the candidates in the 16/17 merge either, but its line is still where the title actually is, so `metadata` points at it directly (`{table: 15, line: 0}`) rather than being forced to reference only the candidates listed in `tables`.

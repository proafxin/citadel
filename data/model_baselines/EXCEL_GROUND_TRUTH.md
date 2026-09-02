# Excel ground truth

The answer key for the harness scorer. Nothing here is derived from any model's output.

Row/col numbers are **sheet coordinates**, re-derived from the current `render_sheet_dump` output in
`data/tabular/dumps/`. Entries marked **[carried]** were not re-derived and may carry coordinate errors from
the previous version of this file.

`test(1).xlsx` sheet1 is covered by `GROUND_TRUTH.md`.

## What a table is

The objective is to **retain the worksheet's data in a queryable form**, not to normalize it into a relational
schema. Blocks that sit in separate positions stay separate tables; nothing is joined to satisfy a modelling
preference.

**A block becomes its own table when it carries both its own header and its own key.** Otherwise it stays with
the block that supplies them, and its label is retained as a band. Splitting is right wherever it is lossless
and wrong wherever it strips a block of its column names or of the column identifying its rows.

| block | own header | own key | outcome |
| --- | --- | --- | --- |
| `1.xlsx` Balancing, three delivery points | yes, row 3 per block | yes, `Hour` at A/E/I | split |
| `8.xlsx` Engines, three stacked tables | yes | yes | split |
| `test(1)` Pivot, three statements | yes, each repeats it | yes | split |
| `1.xlsx` Deals, five bands | no, inherit row 2 | yes | stay, as bands |
| `1.xlsx` Fuel, unit-price bands | no, share row 11 | yes | stay, as bands |
| `2.xlsx` POOL INV VOL, value block | yes, rows 9-11 | no, day is in col 1 | stay |
| `8.xlsx` Engines, measure groups | no | no | stay |

Bands are not a normalization device. They are how a label survives when a block cannot stand alone, which is
what `band_column` is for — it folds the label into a column value so each row keeps its identity without the
split.

1. **A section label is a value, not a boundary.** Stacked blocks sharing one header are one table with
   `bands`; the label belongs in a column, not in a new table.
2. **Different column ranges are different tables when each range stands alone.** A range with its own header
   and its own key is a table. A range that borrows either from its neighbour is part of that neighbour's
   table, because splitting it away leaves rows that cannot be named or identified.
3. **A column that only applies to some bands is a nullable column, not a new table.**
4. **A label/value pair block is a table with two columns.** Reporting it as a `key_value` block instead is
   always an accepted alternative.

## How to score

**The table count is not the target.** A sheet has a fixed set of records, roles and keys, not a fixed right
number of tables. Any decomposition preserving those is correct.

1. **Records.** Every populated data record appears exactly once; nothing that is not a record is reported as
   one. A lost record is the worst outcome.
2. **Roles.** Every row carries the right role. A data row typed as a header names the columns wrong and every
   query against that table is then silently wrong, which nothing downstream can detect or repair.
3. **Keys.** Every persisted row keeps the column that identifies it. A block may be reported as its own table
   only if it carries its own key. A block separated from the column that names its rows is unqueryable, which
   is the one outcome this pipeline exists to prevent.
4. **Packaging.** How blocks are grouped is free. **Any decomposition preserving records and keys is
   correct** — the same rows may come back as one table or as several, and a section label may be carried as a
   band or as a separate table's title. What is wrong is losing a record, dropping a label, or separating rows
   from their key.

**Aggregations are not records.** A block holding only sums over rows a retained table already carries may be
reported as a table, as a block, or folded into the table it totals — all three are correct, because nothing
becomes unrecoverable. This covers `1.xlsx` Deals rows 103-110 and the `Short`/`Long`/`Total` rows at
Balancing 29-31. Totals rows *inside* a table stay with it, since they align to the columns they total.

Side-by-side blocks follow from rule 3 rather than from a rule of their own. `1.xlsx` Balancing splits into
four because each block carries its own `Hour`. `8.xlsx` Engines does not split at its measure groups, because
`EMISSION FACTORS` and `ANNUAL EMISSIONS` have no unit identifier of their own — the `UNIT I.D.` sits in A-D
and is shared.

**On `repeated_groups`:** merging repeats is currently harmful. `materialize_sheet` does not unpivot; it emits
the cell box as a wide grid. A merged Balancing persists as eighteen columns with `Hour` four times and
`Volume`, `Price` and `Position` three times each, told apart only by a `group` attribute, plus a junk `col12`
from the blank separator column. Until something unpivots, the split is the queryable answer.

## Conventions

- **records** = populated data rows, excluding title, header, band-label, total and note rows.
- **header rows** = rows whose cells name the columns. A title row is not a header row.
- **band label rows** = rows carrying a section label, alone or sharing a row with the header.
- **blank template rows** = rows with formulas but no record. Body rows holding null records.

---

# 1.xlsx

## sheet1 'Deals' — 69 + 3 records

Sheet extent `A2:BV114`. Row 3 alone carries a trailing run in cols AG-BV(33-74) with no header — **not** part
of the table. Column E(5) is blank throughout. Rows 17-65 are hidden.

- **deals** — rows 2-97, cols 1-32 (A-AF)
  - header row 2: `Counter Party | Type | Delivery Point | Price` (A-D), hours `1`..`24` (F-AC),
    `Total Volume | Total Billing | Position` (AD-AF)
  - band label rows: 16 `PRESCHEDULED SALES`, 75 `REALTIME SALES`, 78 `PRESCHEDULED PURCHASES`,
    85 `REALTIME PURCHASES`, 91 `ANCILLARIES`; `band_column` null, labels sit alone in column A
  - **69 records**: 3-5, 7 (`IID`, `TNP`, `PNM`, `SPS`), 17-40 (24 `CALPX/Pre/PV`), 42-64 (23 `CALPX/Pre/4C`),
    67-73 (7 named), 80-83, 87-89, 93-96
  - blank template rows: 8-13, 41, 65-66, 74, 76-77, 79, 84, 86, 90, 92, 97
- **epe-totals** — rows 103-110, cols 27-32 (AA-AF) — band labels 103 `Total per Model`,
  108 `Total Ancillary Services Purchases per Model`; **3 records** at 105, 106, 110; no header.
  **Reporting this as a block instead of a table is equally correct.** Every value is a `SUM` over rows the
  deals table already retains, so nothing becomes unrecoverable. Note the ranges encode a grouping the data
  does not — `=SUM(AD16:AD66)+SUM(AD75:AD78)` skips rows 67-74 — but the formulas are persisted with the
  table either way.

Not tables: 14-15, 98, 100 (`Dispatch Note…`), 114 (`Unit or Line Derates/Outages`, nothing beneath).

**Alternative:** `epe-totals` split at row 108 into two tables.

## sheet2 'Preschedule' **[carried]**

Not re-derived; 86,653 tokens, above the single-call budget at every slot size tested.

- rows 1-65, cols 1-27 — title row 1 (a date); header rows 2-3; data 4-65 with band labels (`Purchases`,
  `Sales`, `Incremental Sales`, `GENERATION`, `Imports`) and totals. Must not lose rows 59-65.
- row 67, cols 1-25 — `Unloaded Generation`, 1 record, no header
- row 69, cols 1-25 — `Spin Reserves 7%`, 1 record, no header
- rows 71-75, cols 1-7 — header row 71, 4 records, all values empty
- rows 78-84, cols 1-25 — 4 records, then a sub-block 82-84 with header `MW | W.Ave $`
- rows 86-173, cols 1-27 — `Scratch Area`, no header row

Not tables: 175-183 (prose), 175-176 col 8.

## sheet3 'Fuel' — 12 + 6 + 1 + others

Sheet extent `A1:K55`.

- **san-juan-prices** — rows 5-8, cols 1-3 — title row 5 (`SAN JUAN`), **3 records**
- **waha-prices** — rows 5-7, cols 5-7 — title row 5 (`WAHA`), **2 records**
- **unit-gas-prices** — rows 10-32, cols 1-7
  - header row 11 (`% Fixed | % Spot | MMBtus | Weighted Avg. Price | Cost of Gas Burn` over C-G, unit name
    in B)
  - band labels 10, 17, 21; the three bands **share row 11's header, which is not repeated**
  - **12 records**: 12-14, 18, 22-29; totals 15, 30 (band subtotals), 32 (grand)
- **price-comparison-flags** — rows 20-22, cols 9-10 — note row 20, header row 21 (`Fixed | Spot`), **1 record**
- **supplier-analysis** — rows 34-44, cols 1-11 — titles 34-36, header row 37, **6 records** (38-43), total 44
- **interstate-price-build** — rows 45-50, cols 8-11 — **5 records**, computed result row 50, no header
- **intrastate-price-build** — rows 51-55, cols 1-5 — title row 51, **3 records** (53-55), no header

Not tables: rows 1-3 (update-frequency legend in column C).

## sheet4 'Comp' — 2 records

- rows 8-12, cols 1-24 — header row 8 (hour numbers 1-24), band labels 9 (`Actual`) and 11 (`Dispatch`),
  **2 records** at 10 and 12. Row 12 populates only cols 1-18.
- **must not** be split into two 1-row tables; the hour header belongs to both series

## sheet5 'Economics' — 36 records

Sheet extent `A1:I47`. Every block shares the A-G schema, so it is one relation. `Heat Rate` and `Fuel Cost`
(H-I) apply only to the generation bands and are nullable elsewhere — not a schema change.

- rows 1-47, cols 1-9
  - header rows 1 (B-G) and 26-27 (`Heat | Fuel` over `Rate | Cost`, H-I)
  - band labels 2 `Purchases`, 4 `Incremental Purchases`, 15 `Sales`, 16 `Incremental Sales`,
    27 `Generation` — row 27 shares its row with the H-I header
  - **36 records**: 3, 5-13, 17-25, 28-32, 34-45; total row 47

**Alternative:** the generation block (rows 26-45) as a separate table, since it introduces two columns the
other bands never use.

## sheet6 'spin reserve log sheet' — 17 records

Sheet extent `A1:D46`. Column A holds 17 dates (metadata `A date:17/text:3`; the three text values are the two
instruction rows and the header).

- rows 4-46, cols 1-4 — header row 4, **17 records**, each spanning 1-6 sheet rows because the comment cell
  wraps: 6-8, 10-13, 15-19, 20, 22, 24, 26, 28, 30, 32, 34, 36, 38, 40, 42, 44, 46

Not a table: rows 1-2 (instruction paragraph).

**Hard case:** row 20 opens a new entry (date `2000-04-28`) while its column D text continues the narrative of
the entry above. Grouping 15-20 as one record is a defensible reading.

## sheet7 'Balancing' — 24 × 4 records

Sheet extent `A1:R31`. Column M(13) is blank and separates the load block from the three delivery points.

Four blocks, each carrying its own `Hour` column, so each is independently queryable and each is its own
table. Title row 1 (`Balancing` at A, `Estimated Load` at N), group labels row 2, header row 3.

- **palo-verde** — rows 3-31, cols 1-4 — header row 3 (`Hour | Volume | Price | Position`), **24 records**
  (4-27), totals 29-31 (`Short`, `Long`, `Total`). Row 28 blank.
- **artesia** — rows 3-31, cols 5-8 — same shape, **24 records**, totals 29-31
- **four-corners** — rows 3-31, cols 9-12 — same shape, **24 records**, totals 29-31
- **estimated-load** — rows 3-27, cols 14-18 — header row 3 (`Hour | Forecast | CFE | Sum | Reserves`),
  **24 records** (4-27), no totals

Reporting the first three as one `repeated_groups` table also preserves records and keys and is therefore not
wrong, but it materializes as a wide grid with `Hour`, `Volume`, `Price` and `Position` repeated three times,
so the split is preferred. Column 13 (M) is blank and must not become a column.

## sheet8 'Unit Summary' — 17 + 13 records

Sheet extent `A1:F35`.

- **generation-units** — rows 1-20, cols 1-6 — header rows 1-2 (`Start Up | Minimum Run | Minimum |
  Heat Rates | Heat Rates` over `Generation | Costs | Time (hrs) | Down Time | 0.5 | 1`), band label row 8
  (`LOCAL GEN. ( in order of increasing cost)`), **17 records**: 3-7 and 9-20
- **counter-parties** — rows 22-35, cols 1-2 — header row 22 (`Counter Parties`), **13 records** (23-35),
  first marked `Begin`, last `End`. Row 23's column A is the literal `65535`.

`generation-units` must not be split at row 8; the label is a band and both blocks share the row 1-2 header.

## sheet9 'HeatRate' — 215 records **[carried]**

- rows 1-218, cols 1-13 — header rows 1-3 (unit names over descriptions), **215 records** (4-218, `MWh's`
  1..215)

## sheet10 'Top' — 2 records

Sheet extent `C2:E5`. Structurally identical to Fuel's price blocks: a bold title over label/value rows, with
defined names `StartDt` → `E4` and `EndDt` → `E5` pointing at the values.

- rows 2-5, cols 3-5 — title row 2 (`El Paso Spreadsheet`), **2 records** (4 `Start Date`, 5 `EndDate`),
  no header

**Alternative:** reported as a `key_value` block, i.e. zero tables.

## sheet11 'EPEData' — 0 records

Sheet extent `B1:B61` — a single column. Every row is a comma-delimited string inside one cell, so the
spreadsheet has one column and the tabular structure exists only inside the string values. Row 1 is a serial
timestamp. Nothing here is a table.

## sheet12 'dlgAddRows' — 0 records

Empty.

---

# 2.xlsx

## sheet1 'INPUT SHEET' — 31 records **[carried]**

- rows 5-44, cols 1-19
  - title rows 1-3, plus row 3 cols 9-11 `PROD MONTH | DECEMBER 2000`
  - header rows 5-11 — seven rows: row 5 `PURCH: ENRON CAPITAL` per pool point; row 6 FERC price and
    contract; row 7 BTU factor; row 8 the six pool point names; rows 9-11 spell `DAY/OF/MO` and
    `DLVRD/DAILY/VOLUME` vertically
  - **31 records** (13-43), one per day of December; total row 44
  - 19 columns: a day column plus six groups of three (`DLVRD VOLUME`, `ACTUAL VOLUME`, `DAILY PRICE`)

## sheet2 'POOL INV VOL' — 31 + 2 records

Sheet extent `A1:K47`. One table. Row 8's `INVOICE VOLUMES` at B and `INVOICE VALUE` at H are **column group
labels**, not titles of two tables — the header block at rows 9-11 runs continuously across both sides. This
is the same construct as `8.xlsx` Engines row 8, where `EMISSION FACTORS` over E-I and `ANNUAL EMISSIONS`
over J-N are groups within one table.

The key is `DAY OF MO` in column 1 and only the left group carries it, so the value group cannot stand alone —
those 31 rows would be unidentifiable. `group_name` reads as the measure.

- **invoice** — rows 8-45, cols 1-11 — titles row 8 (`INVOICE VOLUMES` at B, `INVOICE VALUE` at H), header
  rows 9-11 (`DAY/OF/MO`, `DLVRD DAILY VOLUME`, `MINIMUM VOLUME @ FERC`, `ADJ BASE VOLUME @ FERC`,
  `DAILY SHORTFALL`, `REMAINING VOLUME @ DAILY`, then `VALUE @ FERC`, `ACTUAL DAILY VALUE`,
  `TOTAL INVOICE VALUE`, `DAILY PRICE`), **31 records** (13-43), total row 45. Column 7 (G) is blank within
  rows 13-43 and must not become a column.

The previous version of this file split this into cols 1-6 and cols 8-11, which loses the day from the value
table. Every model merged it in every run; they were right and the key was wrong.

- **parameters** — rows 3-4, cols 5-11 — label/value pairs (`First of Month Nominated Volumes` 17000,
  `Tennessee` 0, `Total Days` 31, `Baseload Percentage` 0.75, `Cashout`, `base volume` 12750). **2 records**,
  no header

Not tables: rows 1-2 col 1 (title block), row 47 (loose totals).

## sheet3 'ENRON INV' — 2 records

Sheet extent `A1:H37`. The previous version of this file said zero tables. The line-item area is structurally
a table — a two-row header, two bands, subtotals and a grand total — even though its values are `#REF!`.

- **invoice-line-items** — rows 15-27, cols 1-8 — header rows 15-16 (`MMBTU` over `DRY | RATE | AMOUNT`),
  band labels 18 `TENNESEE GAS PIPELINE` and 22 `NGPL PIPELINE`, **2 records** (19 `Pooling Point`,
  23 `West Cameron 408`), subtotals 20 and 24, grand total 27

Not tables: row 1 (`INVOICE`), rows 3-8 (addressee block), rows 11-12 (prose), rows 33-37 (remit-to block).
Those are `key_value` and prose content.

## sheet4 'FAXES' — 2 records

Sheet extent `A1:J34`.

- **contact** — rows 1-2, cols 7-8 — `Telephone | 405 749-1300`, `Fax | 405 749-6661`. **2 records**,
  no header

Not tables: rows 1-4 col 4 (sender address), row 7 (`FAX COVER SHEET`), and the fill-in-the-blank form rows
11-34 (`To:`, `Company:`, `Phone:`, `CC:`, `From:`, `REMARKS:`) with their filled values. All `key_value`.

**Alternative:** `contact` as a `key_value` block, and the form rows reported as `key_value` blocks carrying
their values.

---

# 8.xlsx

Newly keyed. All five sheets are permit-application emission calculations for the same facility, and all carry
the same three provenance rows: `NORTHERN NATURAL GAS COMPANY`, `ELDORADO COMPRESSOR STATION`,
`TNRCC ACCOUNT NO. SF-0036-Q`.

## sheet1 'Engines' — 4 + 8 + 4 records

Sheet extent `A1:N46`.

- **annual-emissions** — rows 7-17, cols 1-14 — title row 7 (`ANNUAL EMISSIONS`), header rows 8-10 with
  merged `J8:N8`; column groups `EMISSION FACTORS (lb/MMBtu)` (E-I) and `ANNUAL EMISSIONS (TONS/YR)` (J-N),
  each repeating `SO2 | NOX | CO | nm-VOC | PM2.5`. **4 records** (12-15: `E-1`, `E-2`, `E-3`, `A-1`),
  total row 17. `repeated_groups`.
- **hap-factors** — rows 19-30, cols 1-6 — title row 19 (`HAP Emissions AP-42 Factors`), header row 21
  (`Pollutant | 4SRB | 4SLB | 2SLB | Turbines | Gasoline`), **8 records** (22-29), total row 30 (`Total HAP`)
- **potential-hap-emissions** — rows 32-40, cols 1-11 — title row 32, header rows 33-35 with merged
  `C34:G34` and `H34:K34`, **4 records** (36-39), total row 40

Not tables: rows 1-3 (provenance), row 5 (title), rows 42-46 (`NOTES:` and four numbered notes).

## sheet2 'Summary' — 22 records

Sheet extent `A1:H33`.

- **emissions-summary** — rows 5-31, cols 1-8 — title row 5 (`Summary of Emissions`), header rows 7-8
  (`Unit | POTENTIAL TO EMIT` merged `B7:H7`, over `ID | SO2 | NOx | CO | VOC | PM | Formaldehyde |
  Total HAP`), **22 records** (9-30: `E-1`..`A-1`, `TK-1`..`TK-16`, `FUG`, `LOAD`), total row 31

Not tables: rows 1-3 (provenance), rows 32-33 (note).

## sheet3 'Tanks' — 15 records

Sheet extent `A1:I29`.

- **tank-emissions** — rows 5-26, cols 1-9 — title rows 5 (`Storage Tank Potential To Emit`) and 7
  (`TANK EMISSIONS`), header rows 8-10 (`Tank ID | Contents | Capacity | Annual Throughput | Maximum Fill
  Rate | Working Losses | Standing Losses | Annual Emissions | Max Hourly Emissions` with units on row 10),
  **15 records** (11-25, `TK-1`..`TK-15`), total row 26

Not tables: rows 1-3 (provenance), rows 28-29 (note).

## sheet4 'Load' — 1 record

Sheet extent `A1:H17`.

- **loading-emissions** — rows 6-12, cols 1-8 — title row 6 (`Storage Tank Potential To Emit`), header rows
  8-10 (`EPN: | PRODUCT | Mol Wt | AVE. TEMP. | AVG. VAPOR PRESSURE | SAT. FACTOR | ANNUAL THROUGHPUT |
  LOADING EMISSIONS`, units on row 10), **1 record** (12, `LOAD | CONDENSATE`)

Not tables: rows 1-3 (provenance), rows 15-17 (`Note:` and two numbered notes).

## sheet5 'Fugitives' — 9 records

Sheet extent `A1:H26`.

- **fugitive-emissions** — rows 7-23, cols 1-8 — title row 7 (`EPN: FUG       AREA FUGITIVE EMISSIONS`),
  header rows 8-10 (`COMPONENT | COUNT | EMISSION FACTOR *1 | HOURS | PERCENT VOC *2 | ANNUAL (lb/yr) |
  ANNUAL (tn/yr) | DAILY (lb/day)`), band labels 11 `VALVES:`, 14 `PUMP SEALS:`, 16 `FLANGES:`,
  **9 records** (12, 13, 15, 17, 18, 19, 20, 21, 22), total row 23 (`TOTAL VOC (59999)`)

Not tables: rows 1-3 (provenance), rows 24-26 (three starred notes).

**Hard case:** rows 19-22 (`COMPRESSOR SEALS:`, `OPEN ENDED LINES:`, `RELIEF VALVES:`,
`SAMPLE CONNECTIONS:`) are bold exactly like the band labels at 11, 14 and 16, but they carry data and are
records. Boldness alone does not determine role here.

---

# 3.xlsx

## Sheet1 — not keyed

313,915 tokens. Too large to derive by hand.

## Sheet2 — ~1,456 records

Sheet extent `A4:C1783`. A hierarchical SIC classification: division, major group, industry group, industry,
each level a label row, with SIC code and a commoditization rating on the leaf rows only. Record and label
counts were obtained mechanically from the dump, not by reading every row.

- **commoditization-legend** — rows 4-8, cols 1-2 — **5 records** (`HIGHLY COMMODITIZABLE` 1 …
  `NON COMMODITIZABLE` 5), no header
- **sic-commoditization** — rows 12-1783, cols 1-3
  - header row 12: `SIC | COMMODITIZATION` in B-C. Column A of that same row carries the first division
    label (` 0 AGRICULTURE, FORESTRY, AND FISHING`), so the header row doubles as a band label
  - **~1,456 records** — rows with all three of A, B and C populated
  - **~130 band label rows** — rows with only column A populated, at four nesting depths
  - `band_column` is null; labels sit in column A on their own rows

**Hard case:** band labels and records share column A, distinguished only by whether B and C are populated.
Indentation carries the nesting depth and is the only signal of which level a label belongs to.

## Sheet3 — 0 records

Empty.

---

# 4.xlsx

## Sheet1 — not keyed

82,589 tokens. Not yet derived.

---

# 5.xlsx

## Sheet1 — 7 records

Sheet extent `A1:D25`. Rows 1-2 are merged across `A1:D1` and `A2:D2`.

- **nymex-expiration-schedule** — rows 4-21, cols 1-4
  - header rows 4-5: `REFERENCE` (D4) over `TIME | ACTIVITY | EVENT | NUMBER` (row 5)
  - **7 records**, numbered 1-7 in column D, several wrapping across two sheet rows:
    7, 9, 11-12, 14, 16, 18, 20-21

Not tables: rows 1-2 (titles `NYMEX EXPIRATION DAY PROCESSING`, `OVERVIEW OF MEMBER REQUIREMENTS`),
row 25 (`Version 1.2   10/16/2000`).

**Hard case:** record 2 (row 9) has no value in the TIME column — a blank leading cell inside a record, not a
new band.

## Sheet2 — 0 records

Empty.

## Sheet3 — 0 records

Empty.

---

# 7.xlsx

## Week #16 sAT — 26 + 4 records

Sheet extent `A1:AT40`. Hidden columns A, H, AM-AU; hidden row 40. `B5:E5` merged. Defined name `wins`
covers `$I$5:$AL$5`, the win/loss marker row, which is empty in this snapshot.

- **placing-legend** — rows 1-4, cols 9-10 — **4 records** (`1st place` … `4th place`), no header
- **pool-standings** — rows 6-35, cols 1-46
  - header row 6: `Old Rank | New Rank | Prior Total | New Total | Weekly Total | Lost` (C-H), then 30 team
    columns `MIAMI`…`INDY` (I-AL), then `PIT/ST | PIT/NO | BAL/ST | BAL/NO` (AN-AQ) and `STL | NO` (AS-AT)
  - **26 records** (7-32), one per entrant; column A carries a rank for six of them
  - total rows 34 (`TOTAL`) and 35 (`AVERAGE`)

**Hard case:** row 40 (`Edge`) is a hidden record sitting below the total rows, outside the block the totals
summarise. Including it as a 27th record or excluding it as out-of-table are both defensible.

---

# test(1).xlsx

## Sheet1 — not keyed

1,342,265 tokens, 107,786 cells. Cannot be hand-derived from the dump; needs a mechanical approach.
Partially covered by `GROUND_TRUTH.md`.

## Pivot Table_Sheet1_1 — 180 + 17 + 41 + 36 records

Sheet extent `A1:S181`. Two unrelated regions side by side: a country pivot in cols A-C and three stacked
Coca-Cola financial statements in cols G-S. Column D-F are blank and separate them.

- **country-pivot** — rows 1-181, cols 1-3 — header row 1 (`Country | Year | Sum - Total`),
  **179 records** (2-180, `Afghanistan`..`Zimbabwe`), total row 181 (`Total Result`, 11784)
- **coca-cola-pnl** — rows 7-25, cols 9-19 — title row 7 (`Profit & Loss statement`), header row 8
  (`in million USD | FY '09` … `FY '18`), **17 records** (9-25)
- **coca-cola-balance-sheet** — rows 27-69, cols 9-19 — title row 27 (`Balance Sheet`), header row 28
  (identical to row 8), band label rows 29 (`Assets`) and 50 (`Liabilities`) — both carry a label in column I
  and no value — **39 records** (30-49, 51-69)
- **coca-cola-cash-flow** — rows 71-108, cols 9-19 — title row 71 (`Cash Flow statement`), header row 72
  (identical to row 8), **36 records** (73-108)

Not tables: row 5 (`COCA | COLA | Data`), row 6 (`Some data about coca cola`).

The three statements are separate tables rather than one table with three bands **because each repeats its own
header row**. Rule 1 folds stacked blocks together only when they share a single header.

**Alternative:** the three statements as one table with three bands, since their column schemas are identical.

**Hard case:** the 180-row pivot and the financial statements occupy the same rows and would be merged by any
reading that segments only vertically.

---

## Known-hard cases

| case | expected | why it is hard |
| --- | --- | --- |
| `1.xlsx` Deals | one table, 5 bands, 69 records | five section labels under one header |
| `1.xlsx` Fuel | bands 2 and 3 inherit row 11's header | the header is not repeated |
| `1.xlsx` Fuel | 7 blocks across distinct column ranges | side-by-side at rows 5-8, 20-22, 45-50 |
| `1.xlsx` Comp | one table, hour header shared | two 1-row series under one header |
| `1.xlsx` Economics | one table, 36 records | nullable H-I columns must not force a split |
| `1.xlsx` spin log | 17 entries, not 43 sheet rows | comment cells wrap across rows |
| `1.xlsx` Balancing | four tables at cols 1-4, 5-8, 9-12, 14-18 | each block carries its own `Hour`, so each stands alone |
| `1.xlsx` Unit Summary | one table across the `LOCAL GEN.` label | band, not boundary |
| `1.xlsx` EPEData | 0 records | delimited text inside single cells |
| `2.xlsx` POOL INV VOL | one table at cols 1-11 | row 8 labels are column groups, not two titles; only col 1 carries the day |
| `2.xlsx` ENRON INV | one table, 2 records | a document that contains a real table |
| `8.xlsx` Fugitives | 9 records; bold is not a role signal | data rows bolded like band labels |
| `8.xlsx` Engines | three tables, distinct column ranges | stacked, each with its own header |
| `3.xlsx` Sheet2 | ~1,456 records under ~130 band labels | labels and records share column A; only the presence of B and C separates them |
| `5.xlsx` Sheet1 | 7 records, several wrapping two rows | a record with a blank leading cell is not a new band |
| `7.xlsx` Week #16 | 26 records, 30 team columns | hidden columns and a hidden trailing record below the totals |
| `test(1)` Pivot | 179 records plus a total row, beside three stacked statements | same rows, different columns; vertical-only segmentation merges them |
| `test(1)` Pivot | three statements, not one banded table | each repeats its own header — rule 1 does not apply |

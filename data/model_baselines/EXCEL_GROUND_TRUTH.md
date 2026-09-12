# Excel ground truth

Established by reading each sheet's cell grid directly (openpyxl, values + formulas + bold), not from any model output. Source: `/home/masterkenway/Downloads/ocr_input/*.xlsx`. `10.xls` is out of scope (xlsx/xlsm only).

Row and column references are Excel's own (1-based rows, letter columns).

## Conventions

- `header_rows` — only rows that **name columns**. A row above them populating just some columns (`5.xlsx` row 4 `REFERENCE`, `Balancing` row 2 `Palo Verde`, `INPUT SHEET` rows 5-8) is **metadata associated with the table**, not a header row and not a title — we do not pigeonhole it.
- A column holding real per-row data but no name in the header row stays **in the span** and gets a generated name (`col3`). Absence of a header does not make data into a label.
- Content belonging to no table is captured as **text and kept as textual evidence in the pipeline**, so it is still retrievable. Nothing is dropped.
- `data_start` / `data_end` — first and last data row. Aggregation rows (TOTAL, AVERAGE, subtotals) count as data. Blank rows inside a table do not end it and are not listed.
- `span` — column extent, taken from the header. It is the table's schema and does not vary down the table. Columns holding only labels or annotations are **not** widened into the span; they are **associated** with the table as metadata. Pulling them in would impose structure the sheet does not have.
- Populated cells outside a table's span are listed under `uncovered`. Those inside a table's row range are associated with it; the rest belong to no table.
- `title` — the table's own title where the sheet states one.
- Section-label rows inside a table (a label in one column, no data) do not split it.
- Hidden rows and columns **count** — they are real cells and are included in spans and data ranges. They are noted where present, since `read_only` mode cannot see hidden state.
- `PENDING` — a label/value block. Whether these are tables, and in what orientation, is not yet decided.
- `REVIEW` — I could not determine this confidently; needs your call.

---

## 1.xlsx

### Deals — 103 populated rows, last row 114, width A-BV

**T1**
- title: none
- header_rows: `[2]`
- data: 3-97
- span: `A-AF` (32 cols)
- headers: Counter Party, Type, Delivery Point, Price, (unnamed E), 1..24, Total Volume, Total Billing, Position
- notes: rows 14 (`LIST ALL SALES BELOW`), 15 (`leave this row blank`), 16 (`PRESCHEDULED SALES`), 75 (`REALTIME SALES`), 78 (`PRESCHEDULED PURCHASES`), 85 (`REALTIME PURCHASES`), 91 (`ANCILLARIES`) are section labels in column A. They do not split the table.
- hidden: rows 17-65 (49 rows, the CALPX block) are hidden in Excel. Included as data.
- uncovered: row 3 columns `AG-BV` (values 120, 0, 0, 140 …) — no header covers them.

**PENDING** — rows 103-110, label in Z, values in AD/AE (`Total per Model`, `EPE Sales to EPMI`, `EPE Purch from EPMI`, `Total Ancillary Services`).

Non-table: row 100 `Dispatch Note(Copy and paste…)`, row 114 `Unit or Line Derates/…`.

### Preschedule — 176 populated rows, last row 183, width A-AA

**REVIEW.** This sheet has at least seven distinct regions and I am not confident of the boundaries:
- rows 2-3 header (row 2 is all `x`, row 3 is 1..24), data 4-38 ending `Total Obligations`, span A-Y
- rows 39-59 `GENERATION`, with `Capacity`/`MMBtu/Day` appearing as a header for columns Z/AA only at row 40
- rows 60-65 `Imports` … `Total`
- rows 67, 69 single rows (`Unloaded Generation`, `Spin Reserves 7%`)
- row 71 header (`Hourly On-Peak`, `Hourly Off-Peak`, `1 Day On-Peak`, …), rows 72-75 data
- rows 78-84 (`Prescheduled Sales`, `W. Ave. Sale Price`, …), with a `MW`/`W.Ave $` header at row 82
- rows 88-163 `Scratch Area` — repeating name/rate/`Test:`/`Difference:` per unit over columns B-Y. **Either reading is correct**: one table spanning 89-163, or each 4-row unit block as its own table. Both should be scored as acceptable.
- rows 164-173 totals, rows 175-183 recommendations (prose)
- hidden: rows 168 and 173. Included as data.

### Fuel — 48 populated rows, last row 55, width A-K

**PENDING** — rows 5-8, `SAN JUAN`/`WAHA` spot and fixed prices as label/value pairs in A/C and E/G.

**T1**
- title: `Units Using Interstate Gas Prices`
- header_rows: `[10, 11]`
- data: 12-32
- span: `B-G` (6 cols)
- headers: Unit, % Fixed, % Spot, MMBtus, Weighted Avg. Price, Cost of Gas Burn
- notes: rows 15 and 32 are `Total`. Rows 17 (`Units Using Intrastate Gas Prices`) and 21 (`Units Using Either Interstate or Intrastate`) are section labels in column A — **outside the span**, so within B-G they are blank rows and do not split the table. They are **associated** with the table as metadata, not carried into the matrix: incorporating them would be adding semantic structure the sheet does not have.
- uncovered: row 20 col I (`Interstate Cheaper than Intrastate? yes = 1, no = -`), row 21 cols I/J (`Fixed`, `Spot`), row 22 cols I/J values.

**T2**
- title: `ANALYSIS OF CURRENT MARKET` / `February` / `INTERSTATE` (rows 34-36)
- header_rows: `[37]`
- data: 38-44
- span: `A-K` (11 cols)
- headers: Supplier, (unnamed B), Base Price, (unnamed D), Volume, (unnamed F), Rio Grande, Newman, (unnamed I), Total Dollars Rio Grande, Total Dollars Newman

**PENDING** — rows 45-50 (`W.A. Base Price`, `Gross Receipts Tax`, `Fuel Charge`, `Surcharges`, `Variable Price`) and rows 51-55 (`INTRASTATE`, `KN Marketing`).

### Comp — 5 populated rows, last row 12, width A-X

**T1**
- title: none
- header_rows: `[8]`
- data: 10-12
- span: `A-X` (24 cols)
- headers: 1..24
- notes: rows 9 (`Actual`) and 11 (`Dispatch`) are section labels in column A; rows 10 and 12 carry the values.

### Economics — 44 populated rows, last row 47, width A-I

**T1**
- title: none
- header_rows: `[1]`
- data: 2-47
- span: `A-G` (7 cols)
- headers: (unnamed A), Commodity Revenue (Cost), Demand Charge, Total, Total Volume, $/Mwhr Commodity Cost, $/Mwhr Total Cost
- notes: rows 2 (`Purchases`), 4 (`Incremental Purchases`), 15 (`Sales`), 16 (`Incremental Sales`), 27 (`Generation`) are section labels. Row 47 is `Total`.

**T2**
- title: none
- header_rows: `[26, 27]`
- data: 28-45
- span: `H-I` (2 cols)
- headers: Heat Rate, Fuel Cost
- notes: a separate table alongside T1, not part of it. Its header sits at rows 26-27 and its data covers only the `Generation` rows.

### spin reserve log sheet — 29 populated rows, last row 46, width A-D

**T1**
- title: none
- header_rows: `[4]`
- data: 6-46
- span: `A-D` (4 cols)
- headers: Date, Actual 4 minute increment deficiency, Name of person you spoke to, Comments (include what you did)
- notes: rows 1-2 are instructions (metadata). Log entries wrap across rows — rows 7, 8, 11, 12, 13 etc. hold continuation text in B/D only, and are separate data rows.

### Balancing — 30 populated rows, last row 31, width A-R

Four tables. Row 1 (`Balancing`, `Estimated Load`) and row 2 (`Palo Verde`, `Artesia`, `Four Corners`, `Load`, `Spin`) are titles, not header levels. Column M is empty and separates the third block from the fourth.

**T1** title `Palo Verde`, header_rows `[3]`, data 4-31, span `A-D`, headers: Hour, Volume, Price, Position
**T2** title `Artesia`, header_rows `[3]`, data 4-31, span `E-H`, headers: Hour, Volume, Price, Position
**T3** title `Four Corners`, header_rows `[3]`, data 4-31, span `I-L`, headers: Hour, Volume, Price, Position
**T4** title `Estimated Load`, header_rows `[3]`, data 4-27, span `N-R`, headers: Hour, Forecast, CFE, Sum, Reserves

Rows 29-31 (`Short`, `Long`, `Total`) are data rows of T1-T3 only; T4 has no values there.

### Unit Summary — 34 populated rows, last row 35, width A-F

**T1**
- title: none
- header_rows: `[1, 2]`
- data: 3-20
- span: `A-F` (6 cols)
- headers: Generation, Start Up Costs, Minimum Run Time (hrs), Minimum Down Time, Heat Rates 0.5, Heat Rates 1
- notes: row 8 (`LOCAL GEN. (in order of…)`) is a section label.

**NON-TABLE** — rows 22-35, titled `Counter Parties`. A list of counterparty names in column A with `Begin`/`End` markers in column B at rows 23 and 35, and `65535` where row 23's name would be. No structure to speak of; output here can reasonably be anything. Captured as text and retained as textual evidence.

### HeatRate — 218 populated rows, last row 218, width A-M

**T1**
- title: none
- header_rows: `[1, 2, 3]`
- data: 4-218
- span: `A-M` (13 cols)
- headers: Operating MWh's, Newman 1 Steam Unit 1, Newman 2 Steam Unit 2, Newman 3 Steam Unit 3, GT 1 Gas Turbine 1, GT 2 Gas Turbine 2, GT1+ST1, GT2+ST1, Newman 4 GT1+GT2+ST1, Copper, Rio Grande 6 Steam Unit 6, Rio Grande 7 Steam Unit 7, Rio Grande 8 Steam Unit 8

### Top — 3 populated rows, last row 5, width A-E

**PENDING** — rows 4-5 (`Start Date`, `EndDate` with dates in E). Row 2 `El Paso Spreadsheet` is a title.

hidden: column A.

### EPEData — 61 populated rows, width A-B

**T1**
- title: none
- header_rows: `[]`
- data: 2-61
- span: `B-B` (1 col)
- notes: each value is a comma-delimited string (`NEWMAN 1 ,UNAV, -0 ,…`). Recorded as single values; the commas are characters inside the cell, not structure. Row 1 holds a date serial (metadata).

### dlgAddRows — empty, no tables

---

## 2.xlsx

### INPUT SHEET — 42 populated rows, last row 44, width A-S

**T1**
- title: `ENRON ACTUAL SALES ALLOCATION`
- header_rows: `[9, 10, 11]`
- data: 13-44
- span: `A-S` (19 cols)
- headers: DAY OF MO, then DLVRD DAILY VOLUME / ACTUAL DAILY VOLUME / DAILY PRICE repeated six times
- notes: rows 1-3 metadata (`LOUIS DREYFUS NATURAL GAS`, `(ALL VOLUMES ARE IN MMBTU'S)`, `PROD MONTH DECEMBER 2000`). Rows 5-7 hold per-group contract metadata (`PURCH: ENRON CAPITAL`, `FERC 5.925`, `BTU FACTOR`). Row 8 (`ENRON - POOL POINT`, `EAST CAMERON 129`, `HIGH ISLAND 13L`, `HIGH ISLAND 45`, `SOUTH MARSH ISLAND 133`, `WEST CAMERON 408`) is a **label describing the column groups**, not a header row. Row 12 is blank and does not end the table. Row 44 is `TOTAL`.
- consequence: with row 8 excluded from the header, the six column triples get identical names — `DLVRD DAILY VOLUME` appears six times, and nothing in the header distinguishes which pool point a column belongs to.

### POOL INV VOL — 41 populated rows, last row 47, width A-K

**PENDING** — rows 3-4 (`First of Month Nomination|17000`, `Total Days|31`, `Baseload Percentage|0.75`, `base volume|12750`).

**T1**
- title: `INVOICE VOLUMES` / `INVOICE VALUE` (row 8)
- header_rows: `[8, 9, 10, 11]`
- data: 13-45
- span: `A-K` (11 cols)
- headers: DAY OF MO, DLVRD DAILY VOLUME, MINIMUM VOLUME @ FERC, ADJ BASE VOLUME @ FERC, DAILY SHORTFALL, REMAINING VOLUME @ DAILY, (unnamed G), VALUE @ FERC 5.925, ACTUAL DAILY VALUE, TOTAL INVOICE VALUE, DAILY PRICE
- notes: rows 1-2 title. Row 12 blank. Row 44 blank. Row 45 is `TOTAL`.
- uncovered: row 47 (`D=395250`, `H=2341856.25`) — a stray check row after a blank row.

### ENRON INV — 23 populated rows, last row 37, width A-H

**T1**
- title: `INVOICE` (row 1)
- header_rows: `[15, 16]`
- data: 19-27
- span: `A-H` (8 cols)
- headers: (unnamed A-C), MMBTU DRY, (unnamed E), RATE, (unnamed G), AMOUNT
- notes: rows 18 (`TENNESEE GAS PIPELINE`) and 22 (`NGPL PIPELINE`) are section labels; rows 20, 24 are section totals; row 27 is `INVOICE TOTAL`.
- uncovered: rows 3-8 addressee block, rows 11-12 contract reference, rows 33-37 remittance block (`BANK OF OKLAHOMA`, `ABA # 103 900 036`, `ACCT.NO. 209-039-153`).

### FAXES — 16 populated rows, last row 34, width A-J

No tables. A fax cover sheet — label/value form fields (`To:`, `Company:`, `Phone:`, `CC:`, `From:`, `REMARKS:`) scattered across D-J with underscore rules. All content is **uncovered**.

---

## 3.xlsx

### Sheet1 — 2971 populated rows, width A-J

**T1**
- title: none
- header_rows: `[]`
- data: 1-2971
- span: `A-J` (10 cols)
- notes: no header row anywhere — row 1 is already data (`6658, Dole Food Company, 179, Westlake Village,CA, Headquarters, …`). Any header names are invented.

### Sheet2 — 1783 populated rows, width A-C

**T1**
- title: none
- header_rows: `[]`
- data: 4-8
- span: `A-B` (2 cols)
- notes: a legend — `HIGHLY COMMODITIZABLE|1`, `VERY COMMODITIZABLE|2`, `COMMODITIZABLE|3`, `BARELY COMMODITIZABLE|4`, `NON COMMODITIZABLE|5`. Five peer rows, so an ordinary 2-column table, not a label/value block.

**T2**
- title: none
- header_rows: `[12]`
- data: 13-1783
- span: `A-C` (3 cols)
- headers: (SIC code and description), SIC, COMMODITIZATION
- notes: rows 13, 15 and similar are category heading rows carrying no SIC/commoditization values; they are data rows of this table.

### Sheet3 — empty, no tables

---

## 4.xlsx

### Sheet1 — 198 populated rows, last row 199, width A-AY

**T1**
- title: none
- header_rows: `[1]`
- data: 2-3
- span: `B-E` (4 cols)
- headers: x, t, N'(x), N(x)

**PENDING** — rows 5-9 (`S|40`, `K|40`, `r|0.08`, `tau|0.583333`, `sigma|0.35` in A/B; `C`, `Delta`, `Gamma`, `Theta`, `Omega` in D/E; `P` and the same Greeks in F/G), rows 11-14 (`st`, `KB`, `d1`, `d2`), rows 17-21 (`n`, `rb`, `u`, `d`, `p` in A/B and `K`, `Cb`, `Delta` in C/D).

**NON-TABLE** — rows 22-199, titled `Stock Price Tree:` (row 20, col F). A triangular binomial lattice: every row a different-length diagonal, every cell `#VALUE!`, no headers and no row identity. Not queryable as a matrix, so captured as content rather than a table. A model declining to report it as a table is **correct**.

---

## 5.xlsx

### Sheet1 — 14 populated rows, last row 25, width A-D

**T1**
- title: `NYMEX EXPIRATION DAY PROCESSING` / `OVERVIEW OF MEMBER REQUIREMENTS` (rows 1-2)
- header_rows: `[5]`
- data: 7-21
- span: `A-D` (4 cols)
- headers: TIME, ACTIVITY, EVENT, REFERENCE NUMBER
- notes: row 4 holds `REFERENCE` in column D only — metadata, not a header row, so column D is named `NUMBER`. Blank rows 6, 8, 10, 13, 15, 17, 19 sit inside the data and do not end it. Rows 12 and 21 are continuation text in column B.
- uncovered: row 25 (`Version 1.2 10/16/2…`) — a footer.

### Sheet2, Sheet3 — empty, no tables

---

## 7.xlsx

### Week #16 sAT — 34 populated rows, last row 40, width A-AT

**T1**
- title: none
- header_rows: `[]`
- data: 1-4
- span: `I-J` (2 cols)
- notes: four peer rows — a computed value in I against `1st place`…`4th place` in J.

**T2**
- title: none
- header_rows: `[6]`
- span: `A-AT` (46 cols)
- headers: (unnamed A), (unnamed B, holds names), Old Rank, New Rank, Prior Total, New Total, Weekly Total, Lost, then team columns MIAMI, NE, PHIL, SF, TENN, OAK, BUFF, ATL, CLEVE, GB, SD, KC, SEA, NYG, DET, PITT, NO, TB, CHIC, WASH, STL, CZAR, CINCIN, BALT, DAL, AZ, JACK, MINN, NYJ, INDY, (unnamed AM), PIT/ST, PIT/NO, BAL/ST, BAL/NO, (unnamed AR), STL, NO
- data: 7-40
- notes: row 33 is blank and does not end the table. Rows 34 (`TOTAL`) and 35 (`AVERAGE`) are data rows. Rows 36-39 are blank; row 40 (`Edge`, values in F, G, I, K, M, O, Q, S, V, W, Z, AA, AD, AF, AG, AI, AL) is **hidden** in Excel and is included as the last data row — blank rows do not terminate, and its values conform to the columns above.
- hidden: row 40; columns A, H, AM, AN, AO, AP, AQ, AR, AS, AT, AU. All included.
- notes: columns AM and AR carry values in every data row and have no header in row 6 — both are hidden columns.

---

## 8.xlsx

All five sheets share rows 1-3 as metadata: `NORTHERN NATURAL GAS COMPANY`, `ELDORADO COMPRESSOR STATION`, `TNRCC ACCOUNT NO. SF-0036-Q`.

### Engines — 38 populated rows, last row 46, width A-N

**T1**
- title: `ANNUAL EMISSIONS` (row 7)
- header_rows: `[8, 9, 10]`
- data: 12-17
- span: `A-N` (14 cols)
- headers: UNIT I.D. (EPN), TOTAL ANNUAL HOURS, HEAT RATE (Btu/hp-hr), RATED HORSE POWER, then EMISSION FACTORS (lb/MMBtu) × (SO2, NOX, CO, nm-VOC, PM2.5), then ANNUAL EMISSIONS (TONS) × (SO2, NOX, CO, nm-VOC, PM2.5)
- notes: row 5 (`NATURAL GAS FIRED PIPELINE COMPRESSOR ENGINES`) is a sheet-level title. Row 11 blank. Row 17 is `TOTAL`.

**T2**
- title: `HAP Emissions AP-42 Factors` (row 19)
- header_rows: `[21]`
- data: 22-30
- span: `A-F` (6 cols)
- headers: Pollutant, 4SRB, 4SLB, 2SLB, Turbines, Gasoline
- notes: row 30 is `Total HAP`.

**T3**
- title: `POTENTIAL ANNUAL HAP EMISSIONS` (row 32)
- header_rows: `[34, 35]`
- data: 36-40
- span: `A-K` (11 cols)
- headers: UNIT I.D., MMBtu/hr, then Pollutant × (Acetaldehyde, Acrolein, Benzene, Formaldehyde, Methanol, Ethylbenzene, Toluene, Xylene, Total HAP)
- notes: row 40 is `TOTAL`.

Rows 42-46 are notes (metadata), uncovered.

### Summary — 31 populated rows, last row 33, width A-H

**T1** title `Summary of Emissions` (row 5), header_rows `[7, 8]`, data 9-31, span `A-H` (8 cols)
headers: Unit ID, then POTENTIAL TO EMIT × (SO2, NOx, CO, VOC, PM, Formaldehyde, Total HAP). Row 31 is `TOTAL`. Rows 32-33 notes.

### Tanks — 26 populated rows, last row 29, width A-I

**T1** title `TANK EMISSIONS` (row 7), header_rows `[8, 9, 10]`, data 11-26, span `A-I` (9 cols)
headers: Tank ID, Contents, Capacity (gals), Annual Throughput (gal/yr), Maximum Fill Rate (gals/hr), Working Losses (lb/yr), Standing Losses (lb/yr), Annual Emissions (tpy), Max Hourly Emissions (lb/hr). Row 5 is a sheet title. Row 26 is `TOTAL`. Rows 28-29 notes.

### Load — 11 populated rows, last row 17, width A-H

**T1** header_rows `[8, 9, 10]`, data 12-12, span `A-H` (8 cols)
headers: EPN:, PRODUCT, Mol Wt (lb/lb-mol), AVE. TEMP. deg F, AVG. VAPOR PRESSURE psia, SAT. FACTOR, ANNUAL THROUGHPUT gals/year, LOADING EMISSIONS tons/yr. Single data row. Row 11 blank. Rows 15-17 notes.

### Fugitives — 23 populated rows, last row 26, width A-H

**T1** title `EPN: FUG AREA FUGITIVES` (row 7), header_rows `[8, 9, 10]`, data 12-23, span `A-H` (8 cols)
headers: COMPONENT, COUNT, EMISSION FACTOR *1 (lb/hr/comp), HOURS, PERCENT VOC *2, ANNUAL (lb/yr), EMISSIONS ANNUAL (tn/yr), DAILY (lb/day)
notes: rows 11 (`VALVES:`), 14 (`PUMP SEALS:`), 16 (`FLANGES:`) are section labels. Row 23 is `TOTAL VOC (59999):`. Rows 24-26 notes.

---

## test(1).xlsx

### Sheet1 — 5884 populated rows, last row 5927, width A-BS

**T1**
- title: `First excel tables` (row 1)
- header_rows: `[3]`
- data: 4-5
- span: `B-E` (4 cols)
- headers: x, t, N'(x), N(x)

**PENDING** — rows 7-12 (`Inputs:` with `S|60`, `K|70`, `r|0.055`, `tau|0.25`, `sigma|0.22`; `Call Parameters:` and `Put Parameters:` with the Greeks), rows 14-18 (`Calculated Values:` with `st`, `KB`, `d1`, `d2`), rows 22-23 (`Data Source|World Development Indicators`, `Last Updated Date|2026-07-01`).

**T2** — World Bank indicators
- title: `Some data from API download` (row 19)
- header_rows: `[25]`
- data: 26-3045
- span: `B-BS` (70 cols)
- headers: Country Name, Country Code, Indicator Name, Indicator Code, then years 1960 onward
- notes: column A is empty throughout.

**T3** — indicator metadata
- title: `ANOTHER TABLE FROM MET…` (row 3050, col F)
- header_rows: `[3057]`
- data: 3058-4567
- span: `B-E` (4 cols)
- headers: INDICATOR_CODE, INDICATOR_NAME, SOURCE_NOTE, SOURCE_ORGANIZATION

**T4** — orders / shipping. **REVIEW**: extent and span not established.
- title: `SOME SHIPPING DATA` (row 4570, col E)
- header_rows: `[4571, 4572, 4573]` — a three-level header: row 4571 ship modes (`First Class`, `Same Day`, `Second Class`, `Standard Class`), row 4572 segments (`Consumer`, `Corporate`, `Home Office`) under each, row 4573 `Order ID`, `Order Date`
- data: 4574 onward, end not determined

**REVIEW** — rows 5921-5927 hold further content (`Spin Reserves 7%` at 5921, a header at 5923 with `Hourly On-Peak`/`Hourly Off-Peak`/`1 Day On-Peak`/`BOM On-Peak`, then `Greenley`, `PV`, `4 Corners`, `SPS interface`). Not examined.

**Correction note:** this sheet was previously recorded as a single table spanning 26-5927. That was wrong — it was inferred from the head and tail of a dump without reading the middle. Found by the conformance map.

### Pivot Table_Sheet1_1 — 181 populated rows, width A-S

Four tables: one pivot in A-C, and three financial statements stacked in H-S, running alongside it over the same rows.

**T1**
- title: none
- header_rows: `[1]`
- data: 2-181
- span: `A-C` (3 cols)
- headers: Country, Year, Sum - Total
- notes: row 181 is `Total Result`.

**T2**
- title: `Profit & Loss statement` (row 7, col H)
- header_rows: `[8]`
- data: 9-25
- span: `I-S` (11 cols)
- headers: in million USD, FY '09, FY '10, FY '11, FY '12, FY '13, FY '14, FY '15, FY '16, FY '17, FY '18

**T3**
- title: `Balance Sheet` (row 27, col H)
- header_rows: `[28]`
- data: 29-69
- span: `I-S` (11 cols)
- headers: same as T2
- notes: rows 29 (`Assets`) and 50 (`Liabilities`) are section labels. Row 49 `Total assets`, row 69 `Total liabilities and equity`.

**T4**
- title: `Cash Flow statement` (row 71, col H)
- header_rows: `[72]`
- data: 73-108
- span: `I-S` (11 cols)
- headers: same as T2

uncovered: row 5 cols G-I (`COCA`, `COLA`, `Data`), row 6 col G (`Some data about coca…`).

---

## Totals

| workbook | sheets | tables | pending | review |
|---|---|---|---|---|
| 1.xlsx | 12 (1 empty) | 12 | 3 | Preschedule boundaries |
| 2.xlsx | 4 | 3 | 1 | — |
| 3.xlsx | 3 (1 empty) | 3 | 0 | — |
| 4.xlsx | 1 | 1 | 3 | — |
| 5.xlsx | 3 (2 empty) | 1 | 0 | — |
| 7.xlsx | 1 | 2 | 0 | — |
| 8.xlsx | 5 | 7 | 0 | — |
| test(1).xlsx | 2 | 6 | 4 | — |

**35 tables** across 31 sheets, plus 11 pending label/value blocks.

Only `1.xlsx:Preschedule` remains unresolved — its region boundaries outside the Scratch Area are still uncertain.

## Sheets with hidden cells

`read_only` mode cannot detect these, so any implementation that streams sheets will not see them without a second pass.

| sheet | hidden rows | hidden columns |
|---|---|---|
| 1.xlsx:Deals | 17-65 (49) | — |
| 1.xlsx:Preschedule | 168, 173 | — |
| 1.xlsx:Top | — | A |
| 7.xlsx:Week #16 sAT | 40 | A, H, AM, AN, AO, AP, AQ, AR, AS, AT, AU |

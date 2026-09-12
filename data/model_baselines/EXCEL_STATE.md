# Excel table structuring — state, 2026-09-11

Handoff for the rebuild of excel ingestion after the previous path was removed. Every claim here is either a decision the user made or a measurement, with the numbers. Where something is unverified it says so. This replaces the 2026-09-09 version, which described a design and two findings that have since been reversed (see the end of this file).

## Objective

Ingest `.xlsx` / `.xlsm` workbooks and produce, per table, a structure stored as queryable data — so excel content is searchable without a vector index.

User constraints:

- Capture tables as they physically appear, in matrix form. No semantic extraction, no denormalising, no unpivoting, no repair. A wide table stays wide.
- A single table must never be split — a split table returns confidently wrong query answers. Genuinely separate stacked or adjacent tables may be separate.
- Do not teach the model Excel. Only specify the output contract; describing our own rendering notation is allowed.
- Do not send table bodies to the model. Model calls scale with the number of tables, not rows.
- No heuristics. No invented thresholds or classification rules in code.
- Nothing is lost. Every populated cell ends up in a table or as retained text.

## The design

Per sheet, sequentially:

1. **Stage 1 — probe.** Render a window of rows and ask which blocks start in it. The model returns, per block: `kind`, `title`, `header_rows`, `data_start_row`, column span, column names.
2. **Stage 2 — conformance map.** From `data_start`, over the declared span, compute a compact description of every row to the end of the sheet. The model reads it and returns `data_end` plus notes on rows inside the table that are not data.
3. **Coverage.** Mark the claimed rectangle, probe the next unclaimed populated region, repeat until every populated cell is accounted for.

Stage 1 owns the span and `data_start`; stage 2 owns rows. A table has one schema, so stage 2 never revises the span.

Tools exist only for what the model cannot do itself — rows it hasn't seen, exact extents, bookkeeping — not to supply intelligence. They are local functions over the openpyxl grid.

## Settled

### Rendering (stage 1)

One line per row: `row: COL-value [annotations]`. 1-based Excel rows and column letters, so they match what formulas reference. Annotations: the formula, `b` for bold, and a normalised number format (`num`/`currency`/`date`/`percent`). Empty cells render as `COL-`. One-line legend.

`[]` chosen by measurement: it ties `<>` and `()` on cost (~118.8 tok/row), collides with 14 of 155,314 cells versus 6,503 for `()`, and keeps annotations visually distinct from the chat template's `<tool_call>` syntax.

Stage 1 window: 16k tokens.

### Model call

- One call, JSON schema, `enable_thinking: true`, `reasoning_effort: low`, both inside `chat_template_kwargs`.
- **The schema field is `blocks`, not `tables`.** Asking for "tables" made the model drop a non-table block (`Fuel` rows 5–8) that it reported once renamed.
- **`kind` is free-form.** Constraining it to `table|form|title|note` lost information everywhere and dropped whole blocks twice (`POOL INV VOL` parameters, `3.xlsx:Sheet2` legend). Left alone the model uses a more precise vocabulary: `key_value`, `parameters`, `legend`, `section_title`.

### Stage 2: the conformance map

`citadel/tabular/conformance.py`.

- **Reference:** each column's dtype is its first non-empty cell scanning down from `data_start`. Measured across 334 columns in 28 tables: **0** disagree with the column's dominant type, and 143 (43%) are empty at `data_start`, so a single-row reference cannot work.
- **Dtype** comes from openpyxl `data_type`, with one normalisation: a string that parses as a number counts as a number (numbers stored as text). Error cells conform.
- **Row state:** `match` if every populated cell has the reference type, `blank` if nothing is populated in the span, `differ` otherwise with the differing columns named. No sparseness rule.
- **Runs** group consecutive rows by state and deviations, and report the fill range (`n=5-9`). Grouping by exact populated-column set was tried and exploded on wide sparse tables: `test(1):Sheet1` went 2,770 runs / 66k tokens; with state+deviations it is 287 runs / 9.2k.
- Absolute Excel column letters throughout.

Deviating rows inside real table ranges: 20 of 28 tables have none; in 6 more the deviating rows are exactly the totals and section labels a human would point at. The exceptions are genuine mixed data — `3.xlsx:Sheet1` 98 rows (3.3%), `test(1):Sheet1` 1,291 rows (22%).

### Conventions (ground truth)

- **Span = the data columns.** Columns holding only labels are *associated* with the table, never widened into the span.
- **`header_rows` = only rows that name columns.** A row above them populating some columns is metadata associated with the table, not pigeonholed as header or title.
- A column with real per-row data but no header stays in the span with a generated name.
- Section-label rows are not `data_start`; they sit inside the table and do not split it.
- Aggregation rows (totals, subtotals) are data.
- Blank rows do not terminate a table.
- **Forms** (label/value blocks) are one record: transpose to a single row with the labels as headers. Lossless re-orientation, allowed — unlike unpivoting.
- **Content in no table** is captured as text and kept as textual evidence in the retrieval pipeline.
- Hidden rows and columns count as data. Not marked for now, which keeps `read_only` streaming viable.

### Storage

Reuse the existing tabular path: excel produces `list[MaterializedTable]`, `save_sheet_tables` writes one `Table` and N `TableRow` (FK `table_id`, `row_idx`, JSONB `values`). Rows are lossless, header rows are stored and filtered at query time, and `build_table_search_text` makes title, notes and headers searchable. Columns are positional, so `Table.columns` must stay aligned one-to-one with each row's `values`.

### Server

- Model `Qwen3.8-27B-UD-IQ3_XXS.gguf`, `--ctx-size 131072`, `--parallel 4`, reasoning `auto`.
- `reasoning_effort` reaches the model only inside `chat_template_kwargs`; as a top-level field llama.cpp drops it silently. Levels are prompt suffixes, not compute budgets; `medium` injects nothing.
- `xhigh` + JSON schema returns empty content on 8 of 8 sheets, on two servers.
- **Determinism:** sequential requests at one slot are byte-identical (8/8 over 3 runs). Concurrent requests are not — 7 of 8 differed, a 14% spread in generated tokens at `temperature: 0`. **Run comparisons sequentially or they mean nothing.** Concurrency buys 1.8× aggregate throughput, flat beyond 4 slots, at roughly doubled per-request latency.
- Context: worst measured conversation per sheet ~62k (`Preschedule`), median ~5k. 128k is comfortable.

## Measured results

**Model choice.** IQ3_XXS matched UD-Q4_K_XL on structure across every sheet checked and found two tables Q4 missed (`Engines` T3, `Fuel` rows 5–8), at 27% more output tokens and 6.6 GB less VRAM.

**Agentic.** Given a `peek(first_row, last_row)` tool, both quants made well-formed calls with sensible ranges and used the results. `Engines` T2, whose header is outside the window, came back exact.

**Stage 1, all 39 tables, one window each.** 33/39 found, 24/39 exact span, 25/39 exact header rows — raw, against ground truth as it stood. Of the misses: four were ground truth applying a label-row convention since reversed; three were a scorer bug; one (the binomial lattice) is now classified non-table. Genuine errors remaining include `Economics` merging two side-by-side tables into `A-I`, and `Sheet2`/`Week #16` dropping an unnamed data column. Not yet rerun against corrected ground truth.

**Stage 2, 30 tables, fed ground-truth `data_start`.** 15/30, then **18/30** after stating the contract — that the map continues past the table, that an aggregating row belongs to it, and that another table's header or a note block does not. The contract fixed the cases it targeted (`Fugitives`, `POOL INV VOL`, `ENRON INV`, `Fuel`).

## The unsolved problem: stacked tables

Six of stage 2's twelve misses are one table running into the next:

| table | want | got | below it |
|---|---|---|---|
| `Pivot` @9 | 25 | 108 | Balance Sheet, identical I–S columns |
| `Pivot` @29 | 69 | 108 | Cash Flow, identical I–S columns |
| `Engines` @12 | 17 | 40 | narrower tables in A–F and A–K |
| `Engines` @22 | 30 | 40 | table in A–K |
| `test(1)` @3058 | 4567 | 5396 | the shipping table |
| `Sheet2` @4 | 8 | 1783 | the SIC table |

The next table's rows come back `match` because they fit this table's span. Two distinct causes:

- **Identical signatures.** The boundary is visible — `Pivot` row 28 is `differ` on ten of eleven columns with first value `in million USD` — but the contract tells the model a `differ` row may be a total to keep, and it cannot tell that from a header to stop at.
- **A narrower table below a wider one is invisible.** `Engines` T1 spans A–N; the table beneath spans A–F. Within A–N its rows populate A–F with text in A and numbers elsewhere, which matches T1's reference.

## Not built

- Chaining stage 1 into stage 2. Every stage-2 result used ground-truth `data_start`; real stage-1 errors propagating is untested.
- The loop and coverage mask.
- Form transposition.
- Storage gaps: header levels on `Column` (one `group` field today), row-scoped association (`notes` is a flat list), non-table text as a `ContentNode`.

## Ground truth

`data/model_baselines/EXCEL_GROUND_TRUTH.md` — **35 tables across 31 sheets**, read from cells. Testing against it has found errors in it four times: `3.xlsx:Sheet2` recorded as 1,598 rows (it has 1,783); section-label rows recorded as `data_start` (`Comp`, `ENRON INV`, `Fugitives`); `Fuel` span widened to carry a label column; and `test(1):Sheet1` recorded as one 5,900-row table when it holds at least three, one titled `ANOTHER TABLE FROM MET…`. That last one was inferred from the head and tail of a dump without reading the middle.

Still marked `REVIEW`: `test(1):Sheet1` T4 (orders table extent) and rows 5921–5927; `1.xlsx:Preschedule` boundaries.

The corpus is 8 workbooks. It tests that the solution works; it is not a sample of real excel. Rules must generalise from the phenomenon, not from these instances.

## Housekeeping

- The stage-1 scorer matches blocks by `data_start_row` alone, so `Balancing`'s four tables sharing row 4 all match the first.
- `conformance.py` has not been through ruff/mypy (`ruff` is not installed in the project env).
- Nothing from this work is committed.

## Reversed since the previous version

- **"JSON-schema output is much worse than prose."** Only true with thinking off (phantom tables), at `xhigh` (empty), or concurrently on nvfp4. At `low` with thinking on, direct JSON matches prose on 7 of 8 sheets, and extracting from prose in a second call is *worse* — it lost `header_rows` on three sheets and returned nothing for `Balancing`.
- **`citadel/tabular/walk.py`.** Deleted. It inferred column types by sampling and contained invented thresholds. Replaced by the conformance map, which reports and does not decide.
- **"Row signature + stop-ask-resume" as the walk.** Superseded by stage 2 reading a conformance map against a known reference, which removes discovery from the problem.
- **The delimiter result.** `<>` → `[]` appeared to fix `Balancing`, but that comparison ran concurrently and cannot be trusted. `[]` is kept on cost and collision grounds, not accuracy.

## Feedback wanted

1. How should stage 2 separate an injected total from the next table's header when both show as `differ`, without a rule in code?
2. How can a narrower table beneath a wider one become visible, given the map is computed over the wider span?
3. Is one stage-2 call per table affordable at scale alongside stage 1?

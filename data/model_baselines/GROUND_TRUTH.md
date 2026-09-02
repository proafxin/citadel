# Ground truth: test(1).xlsx

Established by directly reading the source cells (not inferred from any model's output). Used to score model baseline outputs in `data/model_baselines/baseline_*/`. Region labels match the filenames produced by `run_excel_full.py` (`sheet{N}_region{I}_r{min}-{max}_c{min}-{max}`).

Total: 13-14 real tables across Sheet1 and "Pivot Table_Sheet1_1".

## Verified, specific regions

- **`sheet1_region2` ("Inputs")** — a genuine single key-value/record table: S, K, r, tau, sigma (Black-Scholes parameters). No header row in the traditional sense; row 0 is a title.
- **`sheet1_region3` ("Call Parameters" / "Put Parameters")** — two column-blocks side by side, no blank column between them (mechanically inseparable by `find_regions`). Row keys match exactly except row 1 (`C` vs `P`, both meaning "the option's price" for their respective side). Correct handling per current design: report as **two separate tables** (never merge in the model's own output) — combining into one wide table with paired columns is a downstream code decision (key-based join), not something the model should do itself, even though it is the more accurate final shape.
- **`sheet1_region4` ("Calculated Values")** — key-value table: st, KB, d1, d2 (values mathematically derived from Inputs via Black-Scholes). Has a trailing junk line ("Some data from API" / "download") that is NOT part of the table and should be excluded or flagged as a note, not as a data row.
- **`sheet1_region5` ("Data Source" / "Last Updated Date")** — tiny 2-row metadata table. **Genuinely unrelated** to `sheet1_region2`/`sheet1_region4` (no logical connection) — used as the clean test case for whether a model recognizes table boundaries absent any legitimate reason to merge.
- **`sheet2_region1`/`sheet2_region2`/`sheet2_region3`** (Profit & Loss statement / Balance Sheet / Cash Flow statement, Coca-Cola) — three genuinely separate real financial statements, vertically stacked in the original sheet but already split into separate regions by `find_regions` (local blank-row detection). Each should read as one clean table: correct title, header row (fiscal years FY'09-FY'18), correct data rows (real line items). Balance Sheet has a **genuine duplicate row** ("Assets held for sale" appears twice, with different year coverage) — this is real data, a faithful model reports it as-is, not as an error to fix or drop.
- **`sheet1_region1`** — small 3-row numeric table: x, t, N'(x), N(x) (normal distribution values).

## Large/sampled regions — not individually verified line-by-line

`sheet1_region6` (~3021 rows, cols 2-71 — one continuous large dataset, cols 2-5 populated almost every row, cols 6+ sparse/varying), `sheet1_region8`, `sheet1_region9`, `sheet1_region12`, `sheet2_region0` (~181 rows, 3 cols — pivot/long-format shaped). These are shown to models budget-sampled, not in full. Judge plausibility of the model's read given what it was actually shown, not full coverage — it cannot report what it never saw.

## Trivial/fragment regions

`sheet1_region0`, `sheet1_region7`, `sheet1_region10`, `sheet1_region11`, `sheet1_region13` — small or single-cell fragments; not independently verified as "real tables" beyond what's visible in their own input text.

## Known model-capability findings from prior testing (context, not to be re-derived each time)

- Free-generation (`call_text`, `enable_thinking: False`, no JSON schema) reliably gets structural judgments right: Call/Put pairing insight, P&L/BS/CF separation, key-value recognition, junk-row exclusion.
- The structured/production path (`call_structured`, JSON-schema-constrained, no thinking channel) reliably gets these same judgments **wrong** on identical input — collapses Call/Put into one non-transposed blob, misreads title-as-header on Inputs/Calculated Values, sets `transposed: false` on clean key-value tables. This is a mechanism problem (missing reasoning channel + rigid schema), not a model-understanding problem.
- Enabling native thinking (`enable_thinking: True`) does not fix this: under JSON schema constraint it never actually engages (reasoning empty, ~80-126 completion tokens); in free text it does engage but is prone to degenerate repetition loops that never converge, and is not even guaranteed to reach the correct judgment once it does reason.
- Table-stacking/boundary recognition is a genuine capability gap, not a mechanism artifact — demonstrated with the Inputs/Data-Source pair (zero legitimate relationship), which the model still merges into "one table" even in free text, across every prompt wording tried. This is the one judgment that needed explicit rules+examples added to the prompt (`prompts/table_structure_excel.md`), unlike the others which just needed the mechanism fixed.

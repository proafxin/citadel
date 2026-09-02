# Excel table structuring — working state

Test file: `/home/masterkenway/Downloads/ocr_input/test(1).xlsx` (18 regions, 2 sheets).
Model: `Qwen3.8-27B-Q4_K_M` via llama.cpp — see `QWEN38_27B_Q4KM_EVAL.md` for serving details and the
baseline evaluation.

## Architecture

`structure_candidate` has two paths:

- `known_table=False` (excel regions, from `extract_sheet_content`) → `_structure_excel`
- `known_table=True` (everything else) → `table_structure_known` + `_TABLE_STRUCTURE_SCHEMA` + `_structure`

`_structure_excel` is **two calls per region, regardless of size**:

1. `table_structure_typed.md` → free-text description of the layout (`call_text`)
2. `table_extract_all.md` → `_EXTRACT_ALL_SCHEMA`, given the rendered lines **and** the description

Then `_entry_structure` per table, and `materialize` on the **whole region grid** — no slicing. All indices the
model returns are region-relative, so `materialize` reads `data_start..data_end` and `header_rows` directly and
takes columns via `col_start` + count.

Rendering (`_structure_text`): full rows when the region has ≤ `FULL_RENDER_CELLS` (400) populated cells,
otherwise `_candidate_text` head+tail sampling within `SINGLE_TABLE_BUDGET` (8192). The rendered header line
carries the true row count, which is why sampled regions still return correct `data_end` (region6 → 3020,
region8 → 1510).

**Columns:** when a region yields one table, the full region width is used and the model's `start_col`/`end_col`
are ignored — this enforces "never drop a column", which the model otherwise violates about a third of the time
by trimming empty leading columns. When a region yields several tables the model's columns are used, because
region3's side-by-side pair genuinely needs `0-1` vs `2-3`.

## Harness

`EXCEL_GROUND_TRUTH.md` (rationale) + `excel_ground_truth.json` (data) + `scripts/excel_score.py` (scorer).
34 expected tables across `1.xlsx` and `2.xlsx`, matched to produced tables by row-span × col-span overlap.

**Baseline, per-region structuring only, no sheet-wide step:**

```
matched=34  missed=0  wrong_rows=13  invented=52
```

Nothing is missed; the failure is over-production. Note `matched` is generous — sheet6 shatters one log into
16 tables and scores 1 match + 15 invented.

Failure classes the score tracks:

- fragments not joined (sheet6: 16 tables for 1; sheet1 `prescheduled-sales` 23 rows not 54)
- label rows emitted as tables (`INTRASTATE`, `SPS | Eddy`)
- data rows used as headers (`headers=['CALPX','Pre','PV']`)
- form fields as tables (`2.xlsx` sheet4: 6 for 1)
- documents producing tables (`2.xlsx` sheet3: expected 0, produced 2)
- a table's other column groups emitted separately (sheet7 `cols 5-8`, `cols 9-12`)

Fixed by the window+scan rebuild: `2.xlsx` sheet1 `sales-allocation` now 31 rows (previously produced
nothing), `2.xlsx` sheet2 `invoice-volumes` 31, `1.xlsx` sheet2 all 6, sheet5 all 4, sheet7 both, sheet9 215.

## The rendered-rows guard (load-bearing)

`_structure_render` returns the text **and** the set of row indices actually rendered. `_entry_structure`
accepts a `metadata_row` only if `row in rendered`. Rows the model was never shown cannot be assigned a role.

Why it exists: on run 2, region12 returned `n_rows=50` with a 129-entry `metadata_rows` list —
`[5, 6, 8, 9, 10, 12, 13, 14, 16, 17, 18, ...]`, the stride pattern of the head+tail sampler, not anything in
the data. 130 of its 180 rows are never rendered, and every one of those indices passed the old
`data_start <= row <= data_end` check. **No code had changed between runs 1 and 2** — the hole was latent and
fired at random. region8 lost 45 records the same way earlier.

The error direction is safe: a genuinely-metadata row that was not rendered is kept as data rather than
silently dropped. No-op on regions small enough to render fully.

## Validated (4 consecutive `sheet_verify.py` runs)

sheet1 = 12 tables, sheet2 = 4 tables, `merge groups=[[0],[1],[2]]`. Every row count identical across all four
runs (region12 was 50 on run 2, before the guard).

| region | result |
| --- | --- |
| region2/3/4 | `'Inputs:'`, `'Call Parameters:'` / `'Put Parameters:'` (2 tables), `'Calculated Values:'` |
| region6 | 3020 |
| region8 | 1510 |
| region9 | 822, `header_rows=[0,1,2]` |
| region12 | 179 |
| region13 | **179** — the `Total Result` row is correctly excluded |
| sheet2 | **4 tables**, merge returned `groups=[[0],[1],[2]]` |

`collapse_live.py` measured the pair live, 3 runs: region3 `n=2` byte-identical, region9
`box=(0,826,0,13) headers=[1,2,3] data=4..825 meta=[826] title_row=0` byte-identical, region6 `data=1..3020`
byte-identical. Only sheet2_region1's *box origin* moved; its row structure was identical every run, which is
harmless because nothing slices on the box.

## Model choice: 27B, measured against the 30B on this exact design

Qwen3-VL-30B-A3B-AWQ was brought back up and run through `collapse_live.py 3` with the **same prompts, same
schema, same regions** as the 27B. Result:

| | 27B Q4_K_M | 30B-A3B AWQ |
| --- | --- | --- |
| region3 | `n=2` 3/3, identical | `n=2` 3/3, identical — **equal** |
| region6 | 1 box 3/3 | 1 box 2/3; **spurious split** `(0,505)`+`(506,3020)` on 1/3 |
| region9 | `headers=[1,2,3]` 3/3 | `headers=[0,1,2,3]` 3/3 — folds the **title row into the header** |
| sheet2_region1 | `data=4..20` 3/3 | `data=4..20` 2/3; **`data=4..16`, 4 rows lost to metadata** on 1/3 |

**region3 was never a model limitation.** The old `run_stage2_raw.py` hand-sliced that region into call/put
halves in the harness (lines 58-62) rather than asking the model to find both tables. Under the current
prompt — the side-by-side fact plus the table count — the 30B splits it 3/3 unaided, exactly like the 27B.
Any claim that the 27B is uniquely able to detect stacked tables is wrong.

The 27B's advantage is **stability and the title/header boundary**, not capability. Both models do the hard
parts; the 27B does them identically every run. The 30B loses 4 rows on one run in three, and its loss is on
*fully rendered* rows — the rendered-rows guard cannot catch it.

**Decision: 27B, because table-structure faults are unacceptable while occasional OCR defects are.** The 30B
is better at OCR (see `QWEN38_27B_Q4KM_EVAL.md`) but that is the cheaper failure here.

## What the collapse replaced

Deleted: `_anchor_boundaries` + worklist, `_starts_to_boxes`, `_detect_boundaries`,
`_detect_boundaries_chunk`, `_type_rows`, `_combine_structure`, `_stage2_structure`, `_apply_scan`,
`_slice_grid`, `_scan_anomalies`/`_scan_profile`/`_scan_conforms`/`_scan_window`/`_row_types`/`_value_class`,
`_first_data_row`, `_title_from_row`, `_render_excel`/`_row_line_excel`/`_chunk_rows`/`_render_excel_chunks`/
`_typing_rows`, `EXCEL_CHUNK_BUDGET`, `STAGE1_MAX_TOKENS`. 301 lines; `structure.py` is 437 lines.

Large regions went from ~10 calls to 2. Scripts testing the old design were deleted with it.

## Open

1. **`header_rows` flips between `[0]` and `[]` on the label/value tables** (region2, region4 — it alternates
   between them run to run). Row counts are unaffected; it only decides whether `'Inputs:'` /
   `'Calculated Values:'` also names a column. The title/header ambiguity seen all session: for a 2-column
   label/value table, row 0 is legitimately both. Two attempts to force it have failed — leave it.
2. **Orphaned prompts** after the collapse: `table_typed_extract.md`, `table_typed_extract_lines.md`,
   `table_structure_anchor.md` (zero references); `table_structure_excel.md` is referenced by four
   `data/model_baselines/` scripts that call `.replace("{%region%}", ...)` on a placeholder that no longer
   exists, so those references are already broken.
3. **Merge still uses `_adjacency_groups`** (exact `(min_col, max_col)` key). With single-table regions now
   getting the full region width, the key is stable — likely why merge finally keeps the three statements
   apart. It has returned `groups=[[0],[1],[2]]` on 4 consecutive runs.
4. Formula and `worksheet.tables` paths remain untestable until the file gains formulas or a native table.

## Prompt recipe (holds across every case tested)

Never teach the model how to read a spreadsheet — no definitions, no signs, no checklists. State what the
input is, what result we want, and the shape of it. **Naming the parts we want is asking for a result**
("its title, heading and note rows"); describing how to recognise them is teaching. Stating a plain fact about
the input is fine and has repeatedly helped.

Load-bearing, each measured:

- "Tables in a region may sit side by side as well as one above another." — without it region3 returns one
  table; with it, 2 tables 3/3 runs.
- Asking for a table **count**. Removing it previously dropped region3 to 0/3.
- Naming title/heading/note rows in the box ask. Dropping the enumeration returned region4 to a trimmed box
  3/3 and cost three other regions their stable full extents.

## Tried and failed — do not retry

- **Offsetting typing output by the box's `start_row`.** Two independent reasons it cannot work: the model
  legitimately points at rows the box excludes (giving negative indices), and every box in a region got
  identical input, so a two-table region received the same answer twice.
- **Passing rendered lines to the extraction call unconditionally.** Cost region8 45 records — extraction read
  the head+tail window as the whole table and marked the visible edges as metadata. The collapsed path avoids
  this because the render header states the true row count.
- **Letting the model group merges over all sheet candidates** (deleting `_adjacency_groups`). Sheet2 went to
  2 tables, every run — it merged all three statements despite distinct titles and non-overlapping rows.
- **Requiring the model to title each merge group.** Still 2 tables; it happily names a container
  ("Financial Statements (Profit & Loss, Balance Sheet, Cash Flow)").
- **Asking for a `transposed` flag.** No false positives on region9/region13, but inconsistent across
  *identical* shapes: region3's halves came back `True` while region2 and region4 — same label/value layout —
  came back `False`, both runs. Acting on it would give different shapes to identical structures in one sheet.
- **Adding rules to the extraction prompt.** "Each row belongs to at most one of these" was both redundant with
  wording already present and false, since metadata rows are inside the data span by definition.

## Root cause pattern

Repeatedly: **the representation passed along had already discarded the signal, and the judgment made on top of
it got blamed.** The fixes that worked were all changes to what reached the model — the full region instead of a
trimmed box, the rows alongside the description, the row index instead of free title text — not changes to how
it was instructed.

Corollary, learned the hard way: the model has been right more often than the plumbing, and it is better
unprompted than under a constraining schema. `run_excel_full.py`'s loose prompt read region9's 3-level header
and Grand Total correctly in one call, which the old multi-stage pipeline never managed.

# Excel table structuring — model comparison

Scored against `EXCEL_GROUND_TRUTH.md`. Last revised 2026-09-01.

## Method

Identical dumps from `scripts/dump_sheets.py`, one identical prompt, run through
`data/model_baselines/run_sheet_bare.py`. Free-form output — no schema, no validators, no repair rounds.

**Measure at temperature 0.** Production sampling is `temperature 0.7, top_p 0.80, top_k 20,
presence_penalty 1.1`. Three findings in the first pass of this work were drawn from single sampled draws and
did not reproduce; they are struck out below. The probe now defaults to `t0` — temperature 0 with
`presence_penalty 0`, since a repetition penalty pushes against an answer that legitimately repeats.

The run script takes a prompt name (`bare` / `conventions` / `shipped`) and a sampler (`t0` / `nopenalty` /
`production`).

## The most important finding

**The answer key was wrong more often than the models were.** Every one of these was a model reading the
worksheet correctly and being scored against a mistaken key:

| sheet | key said | actually |
| --- | --- | --- |
| `2.xlsx` ENRON INV | 0 tables | 1 table, 2 records at rows 15-27 |
| `1.xlsx` Top | 0 tables | 1 table, 2 records, with `StartDt`/`EndDt` defined names on the values |
| `1.xlsx` Unit Summary | 18 records | 17 |
| `1.xlsx` spin log | 18 entries | 17 |
| `1.xlsx` Balancing | totals at rows 28-30 | 29-31; row 28 is blank |
| `test(1)` Pivot balance sheet | 41 records | 39 plus band labels at 29 `Assets` and 50 `Liabilities` |
| `test(1)` Pivot country table | 180 records | 179 plus a total row at 181 (`Total Result`, 11784) |
| `2.xlsx` POOL INV VOL | two tables at cols 1-6 and 8-11 | **one** table; row 8's labels are column groups and only col 1 carries the day |
| `1.xlsx` Balancing | two tables | **four**; each delivery point carries its own `Hour` |

Eight runs were scored against the POOL INV VOL error before it was caught, and prompt wording and dump
enrichment were both changed trying to fix a model that was already right.

## Current best result

**`Qwen3.8-27B-GSQ-RCO-IQ3_XXS`, `shipped` prompt, `t0`** — `baseline_q3gsq_t0_shipped`.

Clean across all 26 sheets. Exact where exactness is checkable:

| sheet | expected | produced |
| --- | --- | --- |
| Fugitives | 9 records, bands at 11/14/16 | 9, bands correct, rows 19-22 typed as data despite being bold like the labels |
| Unit Summary | 17 + 13 | 17 + 13 |
| POOL INV VOL | 1 table, 31 records, cols 1-11 | `A9:K45`, 31 records, blank column G excluded from the column list |
| Balancing | 4 tables, cols 1-4 / 5-8 / 9-12 / 14-18 | 4 peer tables at those ranges, 24 records each |
| Engines | 3 tables | 3 |
| Economics | 1 table, rows 1-47, cols 1-9 | same |
| EPEData | 0 | 0 |

Three remaining differences are all permitted by the key: `epe-totals` reported as a block, Balancing's totals
as their own tables — both aggregation-only, recoverable from retained rows — and the Pivot's three statements
merged, which preserves records and keys.

## Model comparison

At 10.1 GB, GSQ IQ3_XXS matches or beats every larger model tried:

| model | size | notes |
| --- | --- | --- |
| `Qwen3.8-27B-GSQ-RCO-IQ3_XXS` | 10.1 GB | current best; exact on Fugitives, Unit Summary, POOL INV VOL, Balancing |
| `Qwen3.8-27B-UD-Q3_K_XL` | 13.1 GB | equivalent; nests Balancing as `1.1`-`1.4` rather than peers |
| `Qwen3.8-27B-UD-Q4_K_M` | 16.5 GB | **worse** — types two data rows as category headers on Fugitives (7 records, not 9), and visibly degenerates on Unit Summary |
| `google_gemma-4-31B-it-IQ4_XS` | 17.2 GB | equivalent on tables; vision encoder capped at 645,120 px, an architectural regression for the shared OCR path |

**The spread across models is close to nothing, and the smallest is doing fine.** Quantization is not
monotonic — Q4 loses to both Q3 variants on the band-label case.

## Prompt findings

**What demonstrably works**, measured at temperature 0:

- *"A region occupying a single populated column is not a table. Columns are the worksheet's own columns;
  separators inside a cell's text do not make one."* — closed EPEData outright. The model quotes the clause
  and concludes "no tables exist".
- Stating both directions of the stacking rule — blocks under one header are one table with bands, blocks each
  repeating their own header are separate.
- Removing internal vocabulary. The prompt said "text dump"; it now says "you are shown the contents of one
  worksheet".

**What does not work:**

- ~~Dump enrichment~~. A `ROW CELLS` line (per-row populated counts, run-length encoded) and a `BLANK COLS`
  line cost 3.4% of tokens and moved nothing at temperature 0. The model reads them — it cites "the 'Row
  Cells' metadata indicates specific groupings" — and then reaches the same conclusion either way. `ROW CELLS`
  was removed; `BLANK COLS` remains and is superseded by region-scoped computation in `review_structure`.
- ~~`BLANK COLS` was load-bearing for POOL INV VOL~~ — one sampled draw at 0.7, contradicted by every other
  run, and the sheet was never a failure anyway.
- ~~`ROW CELLS` harmed Deals' right edge~~ — one sampled draw.

**Asking for `continues_before`/`continues_after` is worse than not asking.** The model got them right on
clean windows and wrong on `1.xlsx` Preschedule rows 40-80, where it argued with itself across a paragraph and
concluded the table neither continues before nor after — both false. `fill_edges` derives them from window
geometry, so the fields are now pruned from the request schema and the question removed from both prompts.

## Windowing

`baseline_windows_shipped` — `1.xlsx` at rows 100-150 and rows 40-80.

The failure mode that would break stitching — a headerless window promoting its first data row to a header —
**does not occur**:

- HeatRate 100-150 (pure body, no header, `BOLD: -`): *"Header row(s): None visible in this window (headers
  are in row 2, outside the shown range)"*, located via the `HeatRateNames=HeatRate!$A$2:$M$2` defined name.
- Deals 40-80 (opens mid-band, crosses two band labels): headers correctly placed at rows 2-3 outside the
  window via `DFormulaTitles`, band labels at 75 and 78 identified inside it.

Column ranges match the full-sheet runs — `A100:M150` against `A1:M218`, `A40:AF80` against `A2:AF97` — so the
stitch key survives windowing even though column *names* do not. Nothing was fabricated beyond a window's
rows, and all ten empty windows reported nothing.

## Open

- `EXCEL_GROUND_TRUTH.md` marks `1.xlsx` Preschedule and a few others `[carried]` — never re-derived, and the
  reason a window result there could not be judged.
- The rewritten agent (`sheet_structure.py`, `sheet_agent.py`, `sheet_materialize.py`) has never run against a
  sheet. Everything here is the free-form probe, not the shipping path.
- No tool calling is wired. `sheet_tools.py` exposes `find`, `column_values`, `read_range` and `bounds`; only
  its two regexes are referenced anywhere.
- `window_plan` and a stitcher do not exist.

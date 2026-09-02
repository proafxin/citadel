# Qwen3.8-27B Q4_K_M (llama.cpp) — baseline evaluation

Run 2026-08-28. Outputs in `baseline_qwen38_27b_q4km/{excel,ocr,ocr_images}`.
Harnesses: `run_excel_full.py`, `run_ocr_full.py` — both use their existing `LOOSE_PROMPT`, identical to
every stored baseline, so results diff region-for-region and page-for-page.

## Serving

`compose.yaml`: llama.cpp `server-cuda`, `Qwen3.8-27B-Q4_K_M.gguf` (16.6 GB) +
`mmproj-Qwen3.8-27B-bf16.gguf` (0.9 GB), `--ctx-size 65536 --parallel 4` (4 slots x 16384),
`--cache-type-k/v q8_0`, `--chat-template-kwargs '{"enable_thinking": false}'`.

Measured: prefill **1368 tok/s** (6.0s for an 8192-token prompt), decode **32 tok/s**.
Prompt KV is cached per slot — an identical repeat prefills in 143 ms vs 4379 ms cold (30x), and a shared
prefix with a new suffix in 518 ms. Keeping prompt prefixes stable is worth real time.

### Two serving facts that cost a session to find

1. **Thinking must be disabled explicitly.** Without it the model returns `content: ""` with the answer
   stranded in `reasoning_content`, and `_post_qwen` reads `content` — so every call comes back empty.
   `chat_template_kwargs: {"enable_thinking": false}` is a vLLM field and is **ignored** by llama.cpp.
   What works: `reasoning_effort: "none"` per request, or `--chat-template-kwargs` as a server flag.
   A trivial prompt ("reply OK") does not trigger reasoning, so it is not a sufficient health check.
2. **`--ctx-size` is the total pool, divided by `--parallel`.** 131072/8 gave 16384 per slot and left
   **325 MiB free** on a 24 GB card; a *single* image request then killed the server, four times.
   65536/4 keeps 16384 per slot while halving KV, freeing ~3 GB for image encoding. Client-side capacity
   changes do nothing for this — the slot count is a server argument.

Union types in JSON schema (`{"type": ["integer","null"]}`) **work** through llama.cpp's GBNF conversion —
a concern that turned out to be unfounded.

## Excel — 18 regions, all correct

Checked against `GROUND_TRUTH.md`:

- **region2** `'Inputs:'` as section title, 5 data rows
- **region3** side-by-side split (Call cols 0-1 / Put cols 2-3), row 0 as section titles, `C`/`P` read as prices
- **region4** `'Calculated Values:'` title, **`st` kept as a data row**, row 5 as a note
- **region9** title row 0, **3-level header rows 1-3** mapped to 12 Ship Mode x Segment combinations,
  data 4-825, Grand Total footer at 826, and the observation that each order populates only one category
- **sheet2_region2** `'Balance Sheet'` at row 0, row 1 as unit + fiscal years, company inferred from line items
- **region12** identifies FSI 2023 *and* notices it is seeing a sampled subset rather than claiming completeness

region9 was produced from a budgeted head+tail sample, not all 827 rows.

**Cross-model:** on region9, Qwen3-VL-8B, Qwen3.5-4B and 9B all get the gross structure (title, hierarchical
header, pivot shape). The 27B's edge is **boundary precision** — the 9B folds the title into "header rows
0-2", the 27B separates title (row 0) from headers (rows 1-3). That is exactly the distinction our
`header_rows`/`title` fields need, but it is not the step-change the parameter count suggests.

## OCR — 16 documents, 300 dpi, no image-token cap

- `russian_doc_98630` — bilingual Kazakh/Russian header verbatim, e.zan database and Kazakhstan MVD order identified
- `borang_135_malay_form` — `EPE005055893MY`, `28/07/2026 | 11:50:03`, `Lat: 3.11801 / Long: 101.67351`, character-exact
- `handwritten_document` — near-complete on a page the ground truth calls hard for a human
- `business_textbook` — copyright text verbatim, CC BY-NC icons identified

Both misleadingly-named files (`borang_135` is a Pos Laju shipping label; `russian_doc` a ministry order) were
read from content, not filename.

All 16 checked against `OCR_GROUND_TRUTH.md`. Total errors: `affidavit_of_service` stamp validity `31.12.2026`
(is `2028`) and address `33` (is `55`); `zahlentheorie` claims "two columns per page"; `handwritten` reads
`TCBY`/`D` for `RBY`/`J`. Everything else exact, including `transfer_credit_report` — the hallucination trap,
where the page body is blank and the model correctly said so instead of inventing report data.

### Cross-model: the 27B is WORSE at OCR

An earlier version of this file compared **output length** against `qwenvl30b_awq_16doc` and concluded the 27B
was stronger. That was wrong twice over: those baselines used the terse production `page_ocr` prompt, and
length measures padding, not accuracy. Scored against ground-truth tokens instead:

**handwritten_document** (the hardest page):

| model | `RBY 1ST` | `five oto` | `17,07[1?]` | `amounto adjust[ed]` | `April and J` |
| --- | --- | --- | --- | --- | --- |
| **Qwen3-VL 30B** | TCBY | ok | **17,071** | **amounto adjust** | **in April and J** |
| Qwen3-VL 8B | **RCBY** (closest) | ok | 17,077 | adjoh | and Jo |
| Qwen 9B | TRBY | five **olo** | 17,077 | amounts adjuste | in April and J |
| Qwen3.5 4B | **TB4** | **file oto** | 17,077 | amount adjust | in April and J |
| 27B AWQ | TCBY | ok | 17,077 | adjoh | n April and Ju |
| **27B Q4_K_M** | TCBY | ok | 17,077 | adjuto | **and D** |
| GLM-OCR | TRBY | five **ato** | 17,077 | **adjuv** | **and Dec** |

The 30B is the only model to read `17,071` and `amounto adjust` exactly. The 27B Q4_K_M places 6th of 7 — the
only one to misread the month initial. Its AWQ sibling got it right, so that is a quantisation loss.

**affidavit fine print:** 8B and 30B both read `31.12.2028` and `55`; the 27B gets both wrong. The 30B misses
one character in the email, the 8B one in `MAIWP`.

**invoice_864067:** all three exact.

Conclusion: 27B ≈ 6th of 7 on the hardest page and behind the 8B on fine print. It is kept for table
structure, not OCR — see `TABULAR_STATE.md`.

## The consequential finding

**One loose-prompt call read region9 more accurately than the old multi-stage pipeline did.** No anchor
worklist, no two-call typing, no mechanical scan. That collapse was adopted and is now production — see
`TABULAR_STATE.md`.

Note the collapse is **not** a 27B-specific win: the 30B handles the same collapsed design on region3 and
region9 too. The 27B is kept because it is *stable* across runs and gets the title/header boundary right,
while the 30B occasionally loses rows. Details in `TABULAR_STATE.md`.

## Not compared

AWQ INT4 (`cyankiwi`/`barrydeen` repackagings of the same base model, benchmarked Aug 20) — weights deleted,
ruled out as non-viable. `Qwen3.8-27B-UD-Q4_K_S.gguf` (14.3 GB, on disk, never served) is untested; it is the
remaining VRAM lever if image encoding needs another 2.3 GB, with an unmeasured accuracy cost.

# Benchmark Results

> The OmniDocBench and speed sections dated 2026-06-27 describe the **MinerU-era** pipeline and no longer
> describe what runs today (PaddleOCR-VL via vLLM). Kept for history; do not quote them as current.

## vs stock PaddleOCR-VL — 24 real PDFs, 2,175 pages (2026-07-23)

Both sides ran **the same model on the same vLLM server** (`paddleocr-vl`, port 8099, `--max-num-seqs 128`)
on the same GPU, so serving is held constant and only the pipeline differs. Vanilla was run three ways;
the two parallel modes agreed to 0.2%, so 643.1s is its floor, not a harness artefact.

| | wall | note |
|---|---|---|
| vanilla, one document at a time | 743.4s | 17 of 24 files finish in <10s and cannot fill 128 slots |
| vanilla, `predict([...])` | 677.4s | recovers exactly the small-file idle |
| vanilla, 8 threads | 676.3s | agrees with the above — this is the ceiling |
| vanilla, PDFs only | **643.1s** | |
| **citadel** | **413.2s** | **1.56x**, while also building the tree and 106 tables |

The margin is architectural, not faster recognition: born-digital pages never reach the model (a 324-page
book costs 62 crops, not thousands), and admission keeps the GPU fed across document boundaries.

### Text accuracy — scored against each PDF's own text layer

Ground truth is the digital PDFs' embedded characters. 18 documents, 258,246 reference words.

| | weighted recall |
|---|---|
| stock PaddleOCR-VL | **0.9854** |
| citadel | **0.9845** |

**A tie.** It is the same model with the same prompts, so this is the expected result and the honest
headline: *level on reading characters, ahead on turning them into data.* Citadel is better on 10
documents, tied on 6, worse on 2; its largest wins are forms where the text-layer path beats
re-recognition (0.980 vs 0.799, 0.981 vs 0.929). Citadel does **not** score 1.0 despite taking the layer —
~1.5% of layer text is still lost, which is a real open gap in our own path.

Scanned pages have **no ground truth** and are reported only as agreement between the two outputs:
0.991 and 0.968 on the two large books.

### Where citadel is categorically ahead

- **Coverage** — 5 of 35 files are outside stock PaddleOCR entirely (2 legacy `.xls`, csv, json, markdown).
- **Spreadsheets** — `doc2md` reads xlsx losslessly and deterministically (100% cell recall, no GPU), but
  emits **one HTML `<table>` per sheet** with no segmentation, no header detection beyond "row 1 is the
  header" (wrong on the first file tried, whose row 1 is a title), and no types. On a 105-row financial
  sheet holding three statements it produced **one blob of 17 MB / 8.5M tokens — 130x its own model's
  context window — of which 99.94% were empty cells**, because it materialises the sheet's *declared*
  dimensions rather than its data region. Citadel produced three titled, typed, SQL-queryable tables.
- **Model cost at scale** — our structuring payload is capped at 20 rows whatever the sheet's height, so a
  million-row sheet costs the same one call as a hundred-row one.


## OmniDocBench v1.6 — Quality (2026-06-27)

Citadel, **pure-VLM** path (MinerU2.5-Pro core, `CITADEL_GAP_FILL=0`), scored with OmniDocBench's
**official** end2end scorer (`quick_match`, CDM enabled). Full set: **1651 pages, 0 timeouts/errors**.

| Metric | Citadel | Direction |
|---|---|---|
| **Overall** | **95.04** | ↑ higher=better |
| Text edit | **0.040** | ↓ lower=better |
| Formula (CDM) | **95.90** | ↑ |
| Table (TEDS) | **93.25** | ↑ |
| Table (TEDS, structure-only) | **95.84** | ↑ |
| Reading order (edit) | **0.128** | ↓ |

Composite: `Overall = ((1 − TextEdit)·100 + TableTEDS + FormulaCDM) / 3`.

**Config:** pure VLM (no RapidOCR gap-fill), `render_dpi=150`, MinerU2.5-Pro fp8 via vLLM
(`--gpu-memory-utilization 0.6`, `--max-num-seqs 256`, `--mm-processor-cache-gb 2`), predictions
rendered to OmniDocBench conventions (HTML tables, `$$` formulas), scorer env Python 3.11.

**Hardest subsets:**
newspaper (TEDS 0.79), equation_hard (TEDS 0.75), table_hard (TEDS 0.90), handwriting reading-order (0.42).

### vs MinerU2.5-Pro / MinerU2.5 (published, OmniDocBench v1.6)

Our run = **1651 pages → OmniDocBench v1.6** (v1.5 is 1,355 pages). Same official composite.

| Metric | Citadel | MinerU2.5-Pro | MinerU2.5 (1.2B) |
|---|---|---|---|
| **Overall** ↑ | **95.04** | **95.75** | 93.04 |
| Text edit ↓ | 0.040 | 0.036 | 0.045 |
| Formula CDM ↑ | 95.90 | 97.45 | 95.77 |
| Table TEDS ↑ | 93.25 | 93.42 | 87.88 |
| Table TEDS-S ↑ | 95.84 | 95.92 | 91.47 |
| Reading order ↓ | 0.128 | 0.120 | 0.130 |

Sources: MinerU2.5-Pro arXiv:2604.04771 (Table 2) + OmniDocBench v1.6_full repo board · base MinerU2.5 same board.
(Pro paper vs live repo differ slightly: Overall 95.69/95.75, CDM 97.29/97.45 — used the repo numbers.)

### Full leaderboard placement (OmniDocBench v1.6_full)

| Rank | Model | Overall | Type |
|---|---|---|---|
| 1 | MinerU2.5-Pro | 95.75 | open (= citadel's model) |
| 2 | GLM-OCR | 95.22 | open |
| **→** | **citadel** | **95.04** | open (ours) |
| 4 | PaddleOCR-VL-1.5 | 94.93 | open |
| 5 | PaddleOCR-VL | 94.18 | open |
| — | Qianfan-OCR | 93.90 | API |
| — | Gemini 3 Pro / Flash | 92.91 / 92.62 | API |
| — | dots.ocr | 90.77 | open |
| — | Qwen3-VL-235B | 89.78 | open |
| — | GPT-5.2 / GPT-4o | ~86.6 | API |
| — | Mistral OCR | 85.66 | API |

Source: OmniDocBench v1.6_full leaderboard — github.com/opendatalab/OmniDocBench (main).
Not on the board (don't report): AWS Textract, Azure Document Intelligence, Google Document AI, Mathpix, Textin.

## Speed — 19 mixed real docs, same box / GPU / model (2026-06-27)

Both run the **same baked model `MinerU2.5-Pro-2605-1.2B`**, full GPU, same box. Stock =
`mineru -b hybrid-engine --effort high` (VLM + PP-OCR, same shape as citadel's VLM + gap-fill).

| | Citadel | Stock MinerU (hybrid, high) |
|---|---|---|
| Cold (fresh start, first batch) | ~113s | 236s (3m56s) |
| **Warm / steady** (model loaded) | **60.5s** | ~132s |
| Pages | 153 | 145 |
| **Pages/s (warm)** | **2.53** | ~1.1 |

- Steady-state (warm-vs-warm): ~2.2× — 60.5s vs ~132s (2.53 vs ~1.1 pages/s).
- Cold first run: ~2.1× — ~113s vs 236s.
- Deployment (citadel warm vs stock CLI cold every run): ~3.9× — 60.5s vs 236s.

Stock's API caps at 3 concurrent ("Request concurrency limited to 3"); citadel streams 128-wide (peaked at
137 in-flight reqs, GPU KV ~12%). Same model, same GPU. Citadel's warm run is gated by `defence` (73 scanned
pages, gap-fill ~60s); born-digital docs finished in 3–8s. Stock dropped `hearing_iconix.pdf` ("No valid
PDF"), so citadel did more pages (153 vs 145) in less time.

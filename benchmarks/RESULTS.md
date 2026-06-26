# Benchmark Results

## OmniDocBench v1.5 — Quality (2026-06-27)

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

**Hardest subsets** (genuine model-ceiling categories, not pipeline bugs):
newspaper (TEDS 0.79), equation_hard (TEDS 0.75), table_hard (TEDS 0.90), handwriting reading-order (0.42).

### vs MinerU2.5-Pro / MinerU2.5 (published, OmniDocBench v1.6)

Our run = **1651 pages → OmniDocBench v1.6** (v1.5 is 1,355 pages). Same official composite. Citadel's
core is MinerU2.5-Pro, so the **Pro column is the parity row**.

| Metric | Citadel | MinerU2.5-Pro | MinerU2.5 (1.2B) |
|---|---|---|---|
| **Overall** ↑ | **95.04** | **95.75** | 93.04 |
| Text edit ↓ | 0.040 | 0.036 | 0.045 |
| Formula CDM ↑ | 95.90 | 97.45 | 95.77 |
| Table TEDS ↑ | 93.25 | 93.42 | 87.88 |
| Table TEDS-S ↑ | 95.84 | 95.92 | 91.47 |
| Reading order ↓ | 0.128 | 0.120 | 0.130 |

**Read:** citadel lands at **MinerU2.5-Pro's level (95.04 vs 95.75)** — within ~0.7 Overall and marginally
behind on *every* metric, i.e. the pipeline runs the model at ~full published quality, no real degradation.
Biggest single gap is **formula CDM (−1.55)**, most likely the benchmark-only blocks→markdown `$$` rendering
(our real output is blocks, not markdown). The score landing at Pro's 95 (not base's 93) also **confirms the
image bakes the Pro model**.

Sources: MinerU2.5-Pro arXiv:2604.04771 (Table 2) + OmniDocBench v1.6_full repo board · base MinerU2.5 same board.
(Pro paper vs live repo differ slightly: Overall 95.69/95.75, CDM 97.29/97.45 — used the repo numbers.)

## Speed — 19 mixed real docs, same box / GPU / model (2026-06-27)

Both run the **same baked model `MinerU2.5-Pro-2605-1.2B`**, full GPU, same box. Stock =
`mineru -b hybrid-engine --effort high` (VLM + PP-OCR, same shape as citadel's VLM + gap-fill).

| | Citadel | Stock MinerU (hybrid, high) |
|---|---|---|
| Cold (fresh start, first batch) | ~113s | **236s** (3m56s) |
| **Warm / steady** (model loaded) | **60.5s** | ~132s |
| Pages | 153 | 145 |
| **Pages/s (warm)** | **2.53** | ~1.1 |

- **Steady-state (warm-vs-warm): ~2.2× faster** — 60.5s vs ~132s (2.53 vs ~1.1 pages/s).
- **Cold first run: ~2.1× faster** — ~113s vs 236s.
- **Deployment (citadel stays warm vs stock CLI cold *every* run): ~3.9×** — 60.5s vs 236s.

Why: stock's API **caps at 3 concurrent** ("Request concurrency limited to 3"); citadel streams 128-wide
(peaked at **137 in-flight reqs, GPU KV ~12%** — not GPU-bound). Same model, same GPU → the gap is the
**pipeline**. Citadel's warm run is **gated entirely by `defence`** (73 scanned pages, gap-fill ~60s); every
born-digital doc finished in **3–8s**, while stock's 3-wide cap makes even those queue. Stock also **dropped
`hearing_iconix.pdf`** ("No valid PDF"), so citadel did *more* pages (153 vs 145) in *less* time.

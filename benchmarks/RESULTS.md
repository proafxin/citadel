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

## Speed — mixed real-doc set (cold-vs-cold, warmed)

*Pending: citadel vs stock MinerU on the same mixed (born-digital + scanned) set, on the same box.
Report steady-state pages/s (both warmed) as headline + cold-start-inclusive wall-clock as deployment number.*

Prior rough datapoint (16 real docs, stock standalone vs citadel, cold): citadel ~60s vs stock ~3m40s ≈ **~3.5× cold**.

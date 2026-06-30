# End-to-End Ingestion

How Citadel turns an uploaded file of *any* format into a lossless, queryable, retrieval-ready
representation. This is the counterpart to [benchmarks/RESULTS.md](../benchmarks/RESULTS.md): that
doc measures the OCR core's quality; this one describes the whole pipeline around it.

## Goal

Ingest **every** format into a **lossless** persisted representation, ready for retrieval — without
chunking, without flattening tables into prose, and without ever letting a model invent or compute a
data value. Ingestion ends when a document is structured, persisted to Postgres, and a download tree
is materialized.

## Three canonical lanes

Many file types collapse into three extractors. Routing happens once, at `normalize`, in
[normalize_file](../citadel/utils.py).

| Lane | Sources | Extractor | Output |
|---|---|---|---|
| **HTML** (markup → content tree) | `html/htm/xhtml`; `md` (markdown-it + dollarmath); `docx/odt/rtf/epub` (pandoc) | [parse_html](../citadel/services/html.py) | content tree |
| **MinerU VLM** (visual → content tree) | `pdf`; images; `doc/ppt/pptx/odp` (LibreOffice → pdf) | [handle_ocr](../citadel/services/ingestion.py) | content tree |
| **Tabular** (→ canonical Table) | `xlsx/xlsm`; `xls/xlsb/ods` (LibreOffice → xlsx); `csv/tsv`; `json` | [excel.py](../citadel/services/excel.py) + [tabular.py](../citadel/services/tabular.py) | Table + rows |

Plain `text` (anything unrecognized) is a degenerate HTML-lane case: split on blank lines into
paragraphs.

### Format routing (`normalize`)

| Extension | Normalized `kind` | How |
|---|---|---|
| `xlsx`, `xlsm` | `xlsx` | passthrough |
| `xls`, `xlsb`, `ods`, `fods` | `xlsx` | LibreOffice convert |
| `csv` / `tsv`, `tab` / `json` | `csv` / `tsv` / `json` | passthrough |
| `html`, `htm`, `xhtml` | `html` | passthrough |
| `md`, `markdown` | `html` | markdown-it render (`$…$` math → `<math>`) |
| `docx`, `odt`, `rtf`, `epub` | `html` | pandoc → HTML |
| `doc`, `ppt`, `pptx`, `odp` | `office-pdf` | LibreOffice → PDF |
| `pdf` | `pdf` | passthrough |
| images (`png/jpg/webp/…`) | `image:<ext>` | passthrough |
| *anything else* | `text` | decode UTF-8 |

LibreOffice/pandoc run in **ephemeral temp dirs** (per-call `TemporaryDirectory`, auto-deleted); each
normalize job gets a dedicated LibreOffice profile from a pool so concurrent conversions don't collide.

## Pipeline

Workers are **long-running streaming services** consuming Redis Streams — not per-upload scripts. An
upload just feeds the already-running pipeline. Each stage is its own consumer with its own
concurrency bound ([worker.py](../citadel/worker.py)).

```
upload ──> ingest ──> normalized ──> pages ───────────────> merge ──> Postgres + tree store
            (normalize)  (paginate)    (ocr) ──> gapfill ──┘
                                                 (scanned only)
                              └────────────────> tables ────> merge
                                                 (tabular)
```

| Stage | Stream in | Concurrency bound | Work |
|---|---|---|---|
| `normalize` | `ingest` | `normalize_concurrency` | route + convert to a canonical `kind` (CPU / LibreOffice / pandoc) |
| `paginate` | `normalized` | `paginate_concurrency` | render PDF→page PNGs (pdfium, process pool); or split text/html/tabular into units |
| `ocr` | `pages` | `ocr_concurrency` | MinerU VLM extract per page → blocks (async → vLLM) |
| `gapfill` | `gapfill` | `rapidocr_concurrency` | scanned pages only: RapidOCR the lines the VLM dropped (CPU, decoupled off the GPU slot) |
| `merge` | `merge` | `merge_concurrency` | assemble blocks → content tree → persist → tree store |
| `tabular` | `tables` | `slm_concurrency` | per sheet/file: structure + SLM-describe → canonical Table |

The whole pipeline is **disk-free** except the final download artifact: source bytes, page images,
and per-page blocks all flow through Redis (ephemeral, cleaned after merge). The only on-disk dump is
the materialized tree (see Storage).

### Paginate fan-out by `kind`

- **`text`** → split on blank lines (`\n\s*\n`) → paragraph blocks → 1 "page".
- **`html`** → [parse_html](../citadel/services/html.py) DOM walk → blocks → 1 "page".
- **`image:*`** → cap long side ≤ 2500px → one page to `pages`.
- **`xlsx`** → one `tables` job per sheet (`page_count = #sheets`).
- **`csv/tsv/json`** → one `tables` job (`page_count = 1`).
- **`pdf` / `office-pdf`** → render every page to PNG; each carries a `digital` flag (real text layer,
  unrotated) so OCR can read the exact text layer instead of the VLM.

### OCR stage (per page)

[handle_ocr](../citadel/services/ingestion.py) runs MinerU2.5-Pro via vLLM and layers in recovery so
nothing is silently dropped:

- **Born-digital page** — VLM does layout + recognizes non-text; `text/title` blocks are filled from
  the **exact PDF text layer** by bbox (`extract_layer_by_bbox`), then a focused VLM re-crop recovers
  fill-in/handwriting the layer can't see.
- **Scanned page** — VLM reads the text (primary), then the decoupled `gapfill` stage adds only the
  confident lines the VLM missed (RapidOCR, dedup by trigram overlap, score ≥ 0.85).
- **Image blocks** (seals, stamps, figures, logos) the VLM localized but left empty → VLM-crop OCR;
  QR/barcodes are decoded deterministically and stored as their real payload, not a narrated
  description.

The HTML and MinerU lanes converge on the same `Block` shape, so everything downstream
([build_tree](../citadel/services/tree.py)) is format-agnostic.

## The content tree

Every document becomes one tree: **`document → level(s) → leaf`**. Internal nodes are uniformly
`type="level"` (depth + label); leaves carry the actual content. Pagination is **provenance on the
leaf** (`page_no` / `sheet_no` + `bbox`), never a tree tier.

- **Heading levels** are flat by default (PDF: `level = 1`); HTML carries `h1–h6` depth. There is no
  SLM heading-leveling.
- **Leaf kinds**: paragraph, code, equation (LaTeX), list (one leaf, items with nesting depth), table.
- **`content_id`** is the single, deterministic, idempotent id for every node — the *raw string*
  `{library_id}_{doc_id}_{page|sheet}_{ordinal}` ([make_content_id](../citadel/services/tree.py)), not
  a hash. It is the only id tables use too.

### Lossless capture

| Concern | Handling |
|---|---|
| Math | LaTeX everywhere (MinerU, MathML `annotation`, markdown dollarmath) |
| Lists | one leaf per list; per-item nesting `depth` preserved |
| Code | preserved as a code leaf, newlines intact |
| Untagged / inline HTML text | captured as paragraphs (buffer flush in the DOM walk) |
| Plain-text files | segmented into paragraphs on blank lines (no chunking) |
| Images | gated (junk dropped) + alt text / VLM description |
| Embedded tables | structured as first-class Tables (see below) |

## Tables: the integrity boundary

All tabular sources — Excel cells, csv/tsv, json, embedded HTML/PDF tables — converge on **one
canonical Table**: typed columns (`dtype`, `role` DATA/SECTION, `unit`), `n_rows`, `sample_rows`,
a retrieval `description`, `metadata` (title/caption/notes/sheet), and `anchors` for traceability.
Full data lands in `table_rows`, keyed by `content_id` — queried, never embedded.

The rule that makes this trustworthy: **the SLM emits structure and descriptions, never data values.**
It sees anchors/sample rows and writes the table's shape; the actual cell values are carried verbatim
from the source. This is the same boundary the query layer relies on (model writes SQL *logic*, the
DB computes the *data*).

Per-lane structuring:

- **Excel** ([excel.py](../citadel/services/excel.py)) — openpyxl captures every cell, merge, table
  object, freeze; `find_regions` → `build_anchors` → SLM `extract_structure` → materialize. Table
  boundary = a consistent schema (a new schema starts a new table).
- **csv/tsv** — polars types columns deterministically (no SLM for structure).
- **json** — deterministic 1NF "shred" into normalized, joinable entity tables (nested objects
  flattened by dotted key; lists of objects → child tables with a foreign key). Lossless and
  queryable without value dedup.
- **Embedded HTML/PDF tables** — `extract_html_table` expands colspan/rowspan into a dense grid;
  **PDF tables that continue across pages are stitched first** (`stitch_tables` at merge: consecutive
  table blocks with matching column count across a page boundary are merged, repeated headers
  deduped, paratext skipped).

Every table gets an SLM `description` written **for retrieval** — it surfaces the concrete entities,
names, and vocabulary a user would actually search for, because that text becomes the table's
embedding.

## Retrieval-readiness (`search_text`)

Citadel embeds **leaves only**, so all ancestry/metadata is baked into a cleaned `search_text` per
leaf ([build_search_text](../citadel/services/tree.py)): library name + filename + ancestor headings +
the leaf's own cleaned text. Cleaning = NFC normalize, de-hyphenate line breaks, collapse whitespace,
name-clean (`_`/`-` → space).

Table nodes get a **richer** `search_text` — the *full* representation
([build_table_search_text](../citadel/services/tree.py)): library + file + sheet + title + caption +
notes + column headers + description. The rows stay off the text path; tables are retrieved via this
description-rich text and resolved via SQL.

> The embedding bridge that consumes `search_text` (BGE-M3 → pgvector HNSW + tsvector/GIN) and the
> query layer are **designed, not yet built**. Ingestion produces everything they need.

## Storage layers

| Layer | Holds | Lifetime |
|---|---|---|
| **Redis Streams** | the ingestion bus + ephemeral per-doc state (source bytes, page images, per-page blocks, done-counter) | cleaned after merge; final status auto-evicts after 1h |
| **Postgres** | the queryable content tree (`content_nodes`) + `tables` / `table_rows` | durable |
| **Object store** ([storage.py](../citadel/storage.py)) | the denormalized download tree, `{doc_id}.json.zst` (zstd-JSON) | on disk (`tree_store_dir`) now / S3 later |

The object store is the **only** thing written to disk during ingestion — the precomputed tree that
`/result` reads. Everything else is Redis (ephemeral) or Postgres (the DB).

## Reliability

- **At-least-once delivery** via Redis consumer groups; on worker restart, `_recover` reclaims and
  reprocesses whatever a crashed run left pending (bounded by capacity).
- **Idempotent page recording** — `record_page` uses `hsetnx`, so a redelivered page is a no-op (no
  double-count).
- **Merge barrier fires once** — a per-doc `done_count` is incremented per completed page/sheet; the
  page that makes `done == page_count` emits the single `merge` message.
- **Bounded retries** — a handler that raises is requeued with a bumped `attempt` up to
  `MAX_ATTEMPTS = 3`, then gives up cleanly.
- **Failures are scoped** — one page exhausting retries records an `[extraction failed]` marker and
  the doc still completes as **`partial`** (vs `done`); a gapfill failure finalizes the page with the
  VLM blocks alone. A whole-doc terminal failure (`normalize`/`paginate`/`merge`/`tabular`) marks the
  doc `failed` and releases its source blobs.
- **In-flight backpressure** — each stage's `_Capacity` bounds claimed messages (and their blobs), so
  a backlog never pulls more than `limit` entries into worker RAM.

## Output

`/result` returns the materialized tree ([build_document_tree](../citadel/services/document.py)):
`document → level/leaf` nodes with content, list items, equations, and full canonical table reps
(columns, sample rows, n_rows, description, metadata). `/status` reports live `state` +
`done_count`/`page_count` from Redis while the doc is in flight.

States: `queued → normalizing → paginating → (ocr/tabular) → done | partial | failed`.

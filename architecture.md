# Citadel Architecture

How Citadel turns an uploaded file of *any* format into a **queryable relational knowledge layer** — a
lossless content tree plus canonical tables in a relational database — and then answers a question over it
by having the model **write SQL the database executes**, so figures are computed, never guessed. Both
halves — ingestion and query — are built.

The single idea the whole system is organized around: a document should become **data, not searchable
text**. Once it is data, the model is only ever asked to do things a language model is good at — read
layout, judge relevance, write a query — and never the things it is bad at: parsing a messy grid,
aggregating thousands of rows, or inventing a number. Every design decision below follows from holding
that line.

---

## Part I — Ingestion

## Goal

Ingest **every** format into a **lossless**, persisted, *queryable* representation — no chunking, no
flattening tables into prose, and no model ever inventing or computing a data value. The end state is
data, not searchable text: a content tree plus canonical tables in a relational database, ready for SQL.

## Three lanes

Every format reduces to one of three extractors, chosen at intake:

| Lane | Sources | Output |
|---|---|---|
| **Markup** | HTML; Markdown, Word, ODT, RTF, EPUB (converted to HTML) | content tree |
| **Visual** | PDF, images | content tree |
| **Tabular** | spreadsheets; CSV/TSV; JSON | canonical Table + rows |

Slide decks (PowerPoint, ODP) are a fourth path, distinct from all three: they are read directly from their
own shape/text structure (never rasterized to pages), with embedded images OCR'd individually — see
"Reading pages (ocr)" below.

Plain text is a degenerate markup case: split on blank lines into paragraphs.

### What each format becomes

| Source | Lane | How |
|---|---|---|
| spreadsheets (XLSX / XLS / ODS) | tabular | cells read directly |
| CSV / TSV / JSON | tabular | typed parse / structural shred |
| XML | tabular | shredded to JSON (attributes/text folded by the same convention as any XML→JSON tool), then the JSON path |
| HTML | markup | parsed as-is |
| Markdown | markup | converted to HTML (math kept as LaTeX) |
| Word / ODT / RTF / EPUB | markup | converted to HTML |
| PowerPoint / ODP | — | converted to PPTX if needed, read directly from shapes/text, never rasterized |
| PDF | visual | every page rendered to an image and read whole by the vision model |
| images | visual | read as one page |
| SVG | visual | rasterized to PNG, then read as one image — SVG carries no accessible data path today, so a chart authored only as SVG is read the same as any other picture |

## Pipeline

Ingestion runs as streaming workers on a bus — an upload feeds an always-running pipeline, not a
per-upload script. Each phase is an independent, asynchronous consumer running concurrently, and work is
**page-granular**: the unit of work is a page, not a document, so the whole corpus reads in parallel. Every
stage is a FIFO-fed sliding window — it claims only as much as it can hold, and a slot frees the moment one
item completes, never in batches. The phases:

```
                              ┌→ render → rasterize → pages(ocr) ─┐
upload → normalize → paginate ┤                                   ├→ structure → merge → relational store
                              └→ (markup: html/pptx/text blocks) ─┘
                     └──────────────────────────────────→ table_structure ──────→ merge
```

| Phase | Work |
|---|---|
| normalize | route + convert to one of the three lanes |
| paginate | for the visual lane: count PDF pages and emit one render job each. For the markup lane: read HTML/PPTX/plain-text structure directly (no render/ocr — a format-specific parser produces blocks synchronously). For the tabular lane: emit one `table_structure` job per unit (one per spreadsheet sheet, one for a whole CSV/TSV/JSON file) |
| render | rasterize one PDF page |
| ocr | read the whole rendered page image with the vision model → blocks, including any table on that page in the same pass (see "Reading pages" below) |
| structure | a document's blocks (from either the visual or the markup lane, once every page/unit is in) → paratext split, reclassify, stitch tables; resolve each candidate table's structure — mechanically for a table the vision model already read (see below), or with one model call per bounded table for a markup-sourced table whose header/orientation the markup alone did not state — then merge |
| table_structure | tabular lane only (xlsx/csv/tsv/json) — one job per unit; for a spreadsheet sheet, the model sees every candidate at once and structures each real table, drops the spurious ones, and merges those that are one table split apart, because a raw cell range is boundary-ambiguous in a way nothing else in the pipeline is; CSV/TSV/JSON take no model call at all |
| merge | assemble the content tree from the prepared blocks and the finished tables → persist |

**Two structuring mechanisms exist, and which one applies is decided by whether a table's boundaries were
ever in question — not by its source format.** A spreadsheet's raw cell range is the one place boundaries are
genuinely undecidable, so it alone goes through the `table_structure` job: several candidates at once, one
call, the model deciding membership as well as shape. Every other table — from a rendered page or from
markup — is already exactly one bounded table by construction (the vision model already segmented it when it
read the page; a `<table>` tag or a PPTX table/chart object already has hard edges), so structuring it is a
`structure`-phase concern, resolved inline, with no candidate list to build or job to queue: mechanically, by
reading whatever header signal the source already gave, for a table the model already saw once visually;
with one small model call per table, for a markup-sourced table whose *internal* shape (header rows,
orientation, section labels) the markup itself left open.

A document with no tables needing a model call merges as soon as its pages/units are read, so it is
structured and stored *during* the run rather than waiting behind work it does not have. (Becoming
*searchable* is a later, separate step — the prep phase in Part I's finalize — not part of this pipeline.)

The bus carries only lightweight in-flight state — page images and per-page blocks; the uploaded source is
held once in a doc-keyed store and memory-mapped by each stage that reads it, never copied through the bus
per page.

### What bounds the pipeline

There is no layout detector and no crop step. A page is rendered once, whole, and read once, whole: the
rendered image is sent to the vision model (Qwen, vision-language, served behind an OpenAI-compatible chat
endpoint) as a single request asking for the page's content directly. This replaced an earlier two-model
detect-then-crop-then-read design — a classifier that tried to skip pages whose text came from the PDF's own
text layer was found unable to reliably rule out tables, lists, or other non-plain-text structure on a
"digital" page, so that fast path was dropped entirely: every PDF page, digital or scanned, goes through the
vision model unconditionally. Splitting a page into per-region crops read separately was retired along with
it.

**Every call class that reaches a GPU-backed model has its own pool, sized to its own cost profile, and every
pool is global — shared across however many `citadel/app.py`, `citadel/worker.py`, or `citadel/slm.py`
processes are actually running, not per-process.** A page-image OCR request carries the full image-token cost
of a rendered page; a table's internal-structure call carries only the text tokens of a bounded grid; a batch
summary or a large query-resolution call carries a much bigger prompt and runs far longer; an embedding call is
small and high-volume. Running all of these under one shared limit means the cheap, fast calls queue behind
the expensive, slow ones for no reason, and a limit sized for one profile starves the others. `GlobalCapacity`
in `citadel/services/capacity.py` is the shared primitive behind all four pools: a Redis sorted set per pool
(`cap:{pool}`), member = the caller's own deterministic dispatch key, score = acquire timestamp. Acquiring is
one atomic Lua script (prune anything older than the pool's stale TTL, then admit only if under the limit) —
correct regardless of how many processes call it concurrently, since Redis is the single source of truth, not
any process's memory. A caller that can't get a slot waits on Redis pub/sub for the pool's release channel
(with a bounded fallback timeout, since pub/sub delivers only to whoever is subscribed at publish time — a
missed message just costs one extra timeout interval, never a stuck wait, because the atomic acquire is always
re-tried regardless of whether a wakeup arrived) rather than polling in a loop. A slot whose holder crashes
mid-call is not leaked forever: the stale-TTL pruning in the same acquire script reclaims it on the next
attempt by anyone.

- **`get_vision_capacity()`** — page rasterization and embedded-image resolution share this one budget, sized
  `OCR_CONCURRENCY + VISION_BUFFER` (32 + 16 = 48 today). A rasterized page's slot is acquired when the page
  is claimed off the rasterize stream and released only once that same page's OCR call resolves — one slot
  spans both phases, keyed by `doc_id:page_idx`, because the two phases are really one occupancy of the vision
  pipeline for that page, not two independent draws. This is what keeps host memory bounded regardless of how
  many documents or pages are in flight: a page cannot be rasterized far ahead of what the model can actually
  read.
- **`get_text_capacity()`** — every text-only table-structuring call draws from this separate budget, sized
  `TEXT_CONCURRENCY + TEXT_BUFFER` (64 + 32 = 96 today): a spreadsheet sheet's candidate-packed call
  (`structure_tables`) and a markup-sourced table's single-table call (`structure_single_table`) both acquire
  a slot here, one per actual model call. Sized higher than the vision pool because a text-structure call is
  materially cheaper and faster per request, not because more of them run in total.
- **`get_text_large_capacity()`** — deliberately small (`TEXT_LARGE_CONCURRENCY + TEXT_LARGE_BUFFER`, 4 + 2 =
  6 today), because these are the opposite profile from text-structure: batch summaries, the per-document
  summary reduction, query resolution/filtering, and evidence-merge calls all carry large prompts and run for
  a while. Sharing one pool with the high-volume text-structure calls would let a finalize burst of many batch
  summaries occupy most of that pool's slots and starve a concurrently-ingesting library's table-structuring —
  a small, dedicated, low-concurrency pool is what keeps a slow/bursty call class from crowding out a fast/
  steady one, in either direction.
- **`get_embed_capacity()`** — embedding calls to the `bge` service, sized `EMBED_CONCURRENCY + EMBED_BUFFER`
  (16 + 8 = 24 today).

A separate, process-local cap (`RENDER_CONCURRENCY`, derived from CPU count) bounds how many *documents* are
concurrently having their pages walked into the render pipeline at all — this one is intentionally not global,
since it bounds CPU-side rendering work local to each process, not a shared GPU-backed model; a document
queued behind it is exactly the intended backpressure, keeping rendered-but-unconsumed pages from piling up in
memory ahead of what the model can actually process.

Beneath all of this sits a bound this system does not control: each inference server's own scheduler admits
requests by available KV-cache/GPU-memory, not by request count, so the number of requests actually executing
concurrently can be well below what's been admitted here — the rest queue inside the server. That queue is
not starvation (the server is fully utilized while it's non-empty) but it does mean a client-side elapsed-time
measurement on an individual request conflates real inference time with however long that request waited
behind others for a slot; getting an honest split requires the server's own per-request queue and inference
timing, not a client-side stopwatch.

The current numbers (32/16 vision, 64/32 text-structure, 4/2 text-large, 16/8 embed) are a deliberate first
split, not yet load-tested against a real corpus at scale; their effect on throughput and host memory is
actively being re-measured against this shape of pipeline, and will be revised from what's stated here once
that measurement exists.

### Reading pages (ocr)

Every page — digital or scanned, dense or sparse — is rendered to one image and read by the vision model in
one request. There is no region split and no per-block routing: the model returns the page's structured
content directly, as markdown, in one pass — headings, tables, lists, code — and that response is parsed
into blocks.

**Genuinely blank pages are detected before the model ever sees them.** A page's rendered image is checked
for near-zero pixel variance first; a page that is blank produces zero blocks without a model call, rather
than being sent for OCR. This exists because a vision-language model asked to read a page with nothing on it
does not reliably say so — it can instead produce fluent, structurally plausible content that is not on the
page at all, and a blank-page guard is the only thing that closes that off entirely rather than depending on
prompt wording alone.

**The model's own meta-commentary is stripped before parsing, not stored as content.** Even when correctly
declining a page (a blank one that slipped past the guard, or one it judges unreadable), the model's response
can carry a preamble or refusal sentence around the actual content — "here is the transcription...", "there
is no visible text on this page" — and a whole-response code-fence wrapper some replies are wound in. Both
are recognized and removed from the raw markdown before it is split into blocks, so a refusal never becomes a
stored content row and a fence wrapper never causes the whole page to be read as one opaque code block by the
fenced-code detection described next.

**The table/list/code distinction is asked for explicitly, in the same pass, rather than left to the model's
default.** The prompt tells the model: tabular or record-like data — including a label/value form, a
transposed layout, or a crosstab — becomes a markdown table shaped by its *true* row/column structure, not a
literal mirror of the visual layout; a list or enumeration that is not tabular data stays a markdown list;
code, a formula, or an algorithm listing is fenced and its line breaks preserved. This matters because a
prompt that only describes what a table should look like, with no equivalent instruction for lists or code,
measurably pushes the model toward tables as its only structured option — a plain numbered list of clauses
would otherwise come back as a table with mostly empty cells, observed and fixed as a real, previously-live
failure mode, not a hypothetical one.

Fenced code blocks are recognized as their own block type by splitting the response on its own fence
markers before the rest of the markdown is parsed, so a page containing a code sample keeps its exact
indentation and line breaks rather than being flattened into a paragraph alongside everything else.

**Embedded images** — inside DOCX, XLSX, PPTX, HTML and EPUB sources, not just PDF pages — go through the same
vision model individually, so a table or text baked into an image is not lost. Before an embedded image is
sent, it is size-filtered (decorative icons/logos below a pixel threshold are dropped rather than OCR'd) and
content-hash deduplicated: identical images that recur across a document (a repeated letterhead or logo) are
read once, and the result is reused for every occurrence rather than re-reading it each time. Both checks
share the same admission budget as page rasterization above, so embedded-image OCR cannot bypass the memory
bound that page rendering is subject to. A standalone SVG file is rasterized the same way a PDF page is and
then read as one image — SVG has no accessible-data path today, so this is the same visual-lane treatment any
picture gets, not a special case.

Markup and visual pages converge on the same block shape, so everything after is format-agnostic.

## The content tree

Each document becomes one tree: **document → sections (nested by heading depth) → leaves**. Sections carry
a depth and a label; leaves carry content. Pagination is **provenance on the leaf** (page/sheet + box),
never a tree level. Every node has a deterministic id of the form `library_document_page_ordinal`; tables
use the same id.

Leaf kinds: paragraph, code, equation (LaTeX), list (one leaf, items with nesting depth), table.

### Lossless capture

The tree is not a summary of the document; it is the document, restructured. Nothing is dropped to make it
searchable:

| Concern | Handling |
|---|---|
| Math | LaTeX everywhere |
| Lists | one leaf per list, per-item nesting depth kept |
| Code | one code leaf, newlines intact |
| Untagged / inline text | captured as paragraphs |
| Plain text | split into paragraphs on blank lines (no chunking) |
| Images | junk filtered; alt / visual-model text kept; text-less images dropped |
| Paratext (header / footer / page number) | not a leaf — folded into that page's leaves' search text; the printed page number kept as text, page position kept as provenance |
| Embedded tables | first-class canonical Tables (below) |

## Tables: the integrity boundary

Every table — spreadsheet cells, CSV, JSON, an HTML table, or a table on a scanned page — becomes one
canonical shape: typed columns (each a header and a type), the full rows, a sample, a description, its
title/caption/notes, and provenance. The full rows
are **stored as relational rows and queried by SQL — never embedded.**

The rule that makes it trustworthy: **the model emits structure and descriptions, never data.** It sees
only anchors — the table's size, its top rows, and a few sampled rows — and returns the shape; every cell
is copied verbatim from the source and column types are inferred from the values. This is the same
boundary the query side relies on: the model writes the query, the database computes the values. It exists
because the failure mode of every "AI reads your spreadsheet" system is the model quietly misreading or
re-adding a number — so the model is never in a position to touch a value.

A second rule decides *when* a model is asked at all, and it splits into two different questions that used
to be conflated: **where do a table's boundaries lie**, and **what is its internal shape**. Boundaries are
genuinely ambiguous only for a **spreadsheet's raw cell grid** — tables can begin anywhere on a sheet,
several can share one, and nothing in the file says which cells belong to which table. That is the one place
a model is shown several candidates at once and asked to decide membership, drop the spurious ones, and
merge fragments of one table split apart.

Internal shape — which rows are the header, whether the table is transposed, where a section label sits —
is a narrower question that a *bounded, single* table can also need answered even when its boundaries were
never in doubt. An HTML `<table>` or a PPTX table/chart is already exactly one table; nothing needs to
decide where it starts or ends. But its *header* is not always in the markup — the same label/value form,
transposed layout, or crosstab shape a scanned page can carry shows up in real HTML and PPTX just as often,
and a mechanical "row 0 is the header" guess gets those wrong the same way it would for a scanned page. So
that one bounded table is still shown to the model — one grid in, one structure out, no candidate list, no
merge/drop logic, because there is nothing to merge or drop. This is a materially cheaper call than the
spreadsheet path: no token-budget packing, no multi-table splitting prompt, because a single already-bounded
table never needs either.

A page read by the vision model is a third case again: the model already saw the table's pixels once, in
the same call that read the rest of the page, so its structure is asked for **in that same pass** — never
re-derived from the page's own OCR output afterward. Sending an already-read table back through a second
model call would be pure reprocessing of content the model had full visual access to the first time.

The routing is therefore by **ambiguity, not by source** — the distinction matters, because a source-shaped
rule invites a private path per format, and the whole point is that there is exactly one. Every table from
every origin converges on a grid, one materializer turns a grid plus a structure into the canonical shape,
and the only thing that varies is whether that structure was read from the format or asked of a model.

Tables are separated **by schema**: a run of rows with consistent columns is one table; a schema change
starts a new one, so a single source table yields **one or more** canonical tables — a stacked invoice
splits into its summary block and its line-items.

A table also has a **layout**, which the structure model reports alongside the columns: *relational* (each
column is a field, each row a record) or *crosstab* (a matrix — one measure spread across many columns
labelled by the header rows, a grid of years across the top with a value in each cell). A crosstab is
**denormalized to long form** at ingestion: the key columns that identify a row, one column per header-row
dimension, and a single value column, so one wide row becomes many narrow ones. A World Development
Indicators sheet of 66 year-columns lands as one relational table of `[Country, Code, Indicator, Code,
Year, Value]` — 82,636 one-value rows — which is what makes it answerable by ordinary SQL. The distinction
matters because a crosstab left wide is unqueryable: "the value for Bangladesh in 2010" is a *column name*,
not a filter, and no `WHERE` can reach it.

**Verified grounding (2026-07-29).** The spreadsheet path was checked cell-for-cell against a deliberately
hard sheet — one tab, 5,594×71, twelve stacked regions: side-by-side parameter blocks, two crosstabs, wide
metadata tables, and divider labels. Every extracted table matched a source region, and the denormalizations
were exact: the WDI crosstab's **82,636** rows equal the count of non-empty source cells (66 year-columns
over ~3,020 rows), a spot value agreed (`VC.IDP.NWDS`/Bangladesh/2008 = `61000` in both), the sales
`Ship Mode × Segment` crosstab's **834** rows equal its non-empty cells, and the FSI table's 179 rows
matched — with the three title/divider cells correctly *not* extracted. No cell invented, none lost. This is
the reference baseline for the tabular path; the failure surface it does **not** cover is header-less form
PDFs (see Known limitations).

By source:

- **Spreadsheets** — every cell, merge, table object and frozen pane is captured; contiguous regions are
  found and each region's structure resolved by the model from anchors. **The one ambiguous source, and the
  only one that costs a structure call.** Column names are still derived from the header rows the model
  points at, never written by it.
- **CSV/TSV** — one schema by construction. The parser handles quoting, embedded newlines and ragged rows,
  skips leading blank lines, and names the columns from the first non-empty row. No model.
- **JSON** — shredded deterministically into normalized, joinable tables: nested objects flattened by
  dotted key, lists of objects into child tables keyed back to their parent. An entity's keys are its
  schema, so there is nothing to infer. No model.
- **Tables on a rendered page (PDF, images)** — the grid is built deterministically from the vision model's
  own OCR output for that page, and its structure came from the same call: the model already read the
  table's shape once, visually, so nothing asks it again from the grid afterward. Tables continuing across
  consecutive pages are stitched into one first — fragments with a matching column count across a page
  boundary merged, repeated headers dropped, page furniture skipped — so a forty-page table is one table.
- **Embedded HTML/PPTX tables and charts** — the grid is built deterministically with spans expanded (a
  PPTX native chart's own category/series/value data is read directly, the same way a spreadsheet cell is —
  no vision, no model, genuinely lossless). Its *structure* — header row(s), orientation, section labels —
  is asked of the model exactly as described above: one bounded table, one call, no candidate list. The
  header is not assumed from markup alone, because `<thead>`/`<th>` marks *that* a row is a header far more
  reliably than it marks that a form-shaped or transposed table needs restructuring rather than a literal
  read.

Where a model *is* asked, it is shown a bounded view and never the table: at most **20 rows** — the top few,
the bottom few, and every Mth in between, with M widened as the sheet grows so the sample spans its whole
height — against **every column**, because the columns are the schema and a header dropped is a schema lost.
A payload is therefore a function of a table's width, never of its length: a five-row sheet and a
five-million-row sheet cost the same call. Sizing that view by a *token budget* instead is a mistake made
and measured — it let a prompt grow to fill whatever window was available, producing 20k-token requests to
decide which rows were headers.

Structure detection over-produces — a block of prose gridded into cells, a figure or a caption read as a
one-row table, a region that is not tabular at all — so membership is not a separate step but part of the
same `table_structure` call described in the pipeline above: shown every candidate at once, the model
structures each real table, drops the spurious ones, and merges the ones that are one table split apart, all
in a single pass. There is no standalone validation call; a spurious grid is dropped in the same response
that structures the genuine ones, before it ever reaches the query side. The model decides *membership*,
never content — the same boundary as structure.

## Search representation

Citadel retrieves **leaves and tables, never chunks**. A leaf's search text is its own cleaned text plus
its context: library, filename, the headings above it, and the page's paratext; cleaning normalizes
Unicode, rejoins hyphen-split words, collapses whitespace, and turns machine names into words. A table's
search text is assembled the same way, from what the table already holds: library, file, sheet, title,
caption, notes, and its column headers. It is built mechanically, not written by a model. Rows stay off the
text path.

Search text is indexed two ways: dense multilingual vectors for meaning, and exact lexical matching
(substring and trigram) for the names, identifiers and codes a meaning vector blurs together. A table is
*found* through its search text and *answered* by SQL over its rows.

## Storage

- A **doc-keyed source store** holds each uploaded file on shared disk for the length of its run — the
  interface a real object store (S3) will later fill. Every stage that renders or reads the source
  memory-maps it from here, so a large file is materialized once, not copied through the bus per page; it
  is deleted when the document completes.
- A **streaming bus** holds the pipeline's in-flight state (page images, per-page blocks) — ephemeral,
  bounded by backpressure so its memory stays flat regardless of upload size, and cleared after merge.
- A **relational database** holds the durable content tree, the tables and their rows, and the search
  indexes — this is what queries run against. The rows of every table are stored together and projected
  into typed columns on demand, rather than materialized as one physical table each: a real corpus has far
  too many tables for a physical table apiece.
- An **object store** holds the assembled per-document tree for download.

## Reliability

- Delivery is at-least-once; a crashed run's pending work is reclaimed and reprocessed on restart.
- Recording a page is idempotent, so a redelivered page is a no-op.
- A single merge fires exactly once — when the last page or sheet of a document completes.
- Work that fails is retried a bounded number of times, then given up cleanly.
- Failures are scoped: a page that can't be read leaves a marker and the document still completes as
  *partial*; a whole-document failure is marked *failed*.

## Output and finalize

The result of ingestion is the materialized tree — sections and leaves with their content, and full
canonical table representations. That is the ingestion contract, and it draws a hard line: **ingestion
produces the queryable *structure*; it does not produce retrieval.** Once the tree and tables exist the
library is **DB-queryable** — SQL runs over the typed tables — but not yet **retrieval-queryable**, which
needs the summaries the resolver reads. Progress is a state per document (`queued → processing → ingested`,
with `failed` / `skipped` for the special cases) and per library (`ingesting → ingested → ready`).

Making a library *answerable* is a **separate prep phase**, run once per library after its last document is
stored — deliberately never interleaved with reading, so it can't contend with the vision model for the GPU.
Everything in it is a *reduction over already-persisted structure*, not structure itself, which is exactly
why it belongs after ingestion rather than inside it:

- **Summarize.** Every document is packed into batches and each batch summarized, then the document's own
  summary is reduced from those — the whole library in one pass. These are what the resolver reads, so a
  library is not retrieval-queryable until they exist.
- **Embed and index.** Each leaf's and table's search text is embedded into the dense index and the lexical
  index is built.

Summaries and embeddings do not depend on each other, so they run **concurrently** and the library is ready
once both land — the summary work is on Qwen, the embedding on a separately served `bge-m3` (below), and
running them together hides the shorter (embedding) under the longer (summaries) instead of stacking the two.
Summaries used to stream per document *during* ingestion, overlapping OCR; that overlap paid a GPU-contention
tax and, worse, let a query in the window resolve over a partial inventory. Running them in this phase, gated
on the library's last document draining, closes both.

**Readiness itself is a durable, crash-recoverable stream job, not an in-process wait.** `citadel/app.py`'s
finalize step no longer blocks on anything — it starts batch-summary jobs, enqueues one embedding job onto
`STREAM_EMBED`, and returns immediately. Two independent consumers in `citadel/worker.py` each record a flag
(`citadel/services/readiness.py`) once their half is genuinely done: the last batch summary to complete
records the `summaries` flag, and the embed job records `embedding` once `embed_library` finishes. Recording a
flag is one atomic Lua script — set the flag, check whether both are now present, and if so emit one entry to
`STREAM_LIBRARY_READY`, consumed by a small dedicated job that does the actual `LibraryStatus.READY` write.
Every piece here is either a Redis Streams consumer-group read or an idempotent Lua script, the same pattern
every other phase in this pipeline already uses — if `app.py` or a worker restarts mid-flight, nothing about
the wait is lost, because nothing was held in a process's memory to begin with.

**A library is blocked from ever reaching ready while any document's batch summary has permanently failed,**
not merely tracked separately from a successful one. This falls out of the completion check itself rather
than needing a distinct failure state: a document only stops counting as "pending" once `summarized_at` is
actually set, and a summary job that exhausts its retries deliberately never sets it — it just logs and gives
up. The library's pending count for that document then never reaches zero, the `summaries` flag for that
library is never recorded, and `STREAM_LIBRARY_READY` never fires — the library stays at `ingested` until the
failure is resolved by hand and batch summarization is re-driven for that document.

**The embedding index is disposable; the tree and tables are not.** Nothing about the content tree or the
canonical tables is specific to any embedding model — they are typed, structured, model-agnostic state,
built once by the ingestion pipeline (Part I) and never touched by the prep phase. Embedding is a pure
reduction *over* that structure, run separately, after it. Swapping the embedding model, or the summarizing
model, therefore means re-running the prep phase against structure that already exists — not re-ingesting a
single source file. The expensive step (OCR, layout, table structuring) is paid once, independent of which
model reads the result afterward.

Finalize keeps a `described` status transition so the phases it reports (and the UI reading them) are
unchanged, but nothing is generated there any more: table structure and validation happen in the table
stage, and there is no separate per-table description — a table's search and evidence text is built
mechanically from what it already holds.

The prep phase is gated by the library's **tier**. A *structure* library is ingested to the lossless tree
and tables and stops there; a *search* library additionally gets the summaries, embeddings and indexes that
Part II runs on. The tier is a pricing/access boundary, not a different pipeline — a structure library can be
upgraded and finalized later without re-ingesting. The phase is event-driven: it fires the moment a library's
last in-flight document drains, and is re-checked on restart for any library that became ready while the app
was down.

---

## Part II — Query

**Built.** One model call resolves the question against the whole library at a uniform grain and returns
what the answer must account for and how deeply each of it must be read. Everything after that executes
that decision: the database computes every figure, and one deterministic budget rule fits both kinds of
evidence into a single answer. This replaced a wide-net pipeline (reformulate → retrieve → filter → compute
→ unify → fit) described, with the reason it failed, at the end of this part.

The hard part is not finding candidates. It is deciding which of a large body of evidence actually bears
on the question, fitting exactly that into one answer, and never letting the model invent a figure or
miscount what fits.

## Query pipeline

```bash
                     ┌─→ tables at full ──→ SQL ──→ execute ──────────────┐
question ─→ resolve ─┤   (model over schema + samples)                    ├─→ synthesize → answer
 (inventory)         └─→ documents ──→ floor, then depth to an equal share┘        (model)
                         (summary / parts / wording)
```

| Stage | Work |
|---|---|
| resolve | over an inventory of every document (its summary) and every table (identity, row count, column names), the model returns which items the answer must account for and at what depth |
| tables | tables resolved as needing values from their rows go to the query writer with full schema and samples; the database runs what it writes |
| fit | every covered item's cheapest form is reserved first; computed rows take what that leaves; depth takes what the rows leave, capped at an equal share per document |
| synthesize | one cited answer from the text evidence and the computed results |

Resolution reads the whole corpus in a compressed representation — one summary per document, identity and
column names per table, never the full content — and may legitimately return nothing. There is **no query
reformulation, no corpus-wide similarity net, no per-section selection pass, and no cross-channel
arbitration**. The reasons are below.

### Batches — the unit of text relevance, built once in the prep phase

The reason batches exist is **amortization**. Deciding which prose bears on a question requires reading
prose, and reading a whole corpus per question is a map-reduce every time — paid again on every query, over
text that never changed. Summarizing once, up front, moves that cost off the query path and makes it
reusable: the corpus is read once, and every subsequent query judges relevance against the stored result.
Query-time map-reduce then becomes the fallback for evidence that genuinely exceeds the window, not the
routine path.

Headings are author-written labels. "Introduction" or "Section 3" carries no signal, so a channel that
judges relevance from headings alone judges from metadata rather than content. Tables have an organic
compressed form — schema and sample rows, the table's own content uninterpreted — but prose has none:
sampled paragraphs are not representative. This is the one place in the system where generated text is
justified.

Each document is packed into **batches**: content blocks in document order are accumulated until a token
limit is reached (`BATCH_TOKENS = 32768`). Each batch is summarized once, in the **prep phase** — after the
library's structure is complete, not during ingestion — one job per document on the stream, drained by the
same bounded-concurrency pool as every other SLM stage. The batch stores its own **block-ordinal and page
range** at creation, so citation is a stored field. Summary length is bounded per batch (`min(10% of content
tokens, 4096)`) so the summaries stay a true reduction. Summaries are a *reduction over persisted structure*,
not structure itself — which is why they run here, behind the same tier gate as embedding, rather than
streaming during ingestion where they contended with OCR and could leave a query a partial inventory.

This gives the hierarchy the query side needs — document summary → batch summary → block — and it means
relevance is judged against content, not labels. A document's own summary is written at the end of the same
job, reduced from its batch summaries once they all exist, to a fixed per-document ceiling: it is that
document's entry in the inventory, and the whole library's entries must fit one call.

### Resolution — coverage and depth, decided once

Membership is decided **once**, for the whole question, with every item visible at the same grain. Each
document appears as its summary; each table as its filename, where in the file it sits, its title or
caption, its row count and its column names — no dtypes, no sample values, because those scale with width
and this view exists to judge bearing, not to write SQL. The model returns the items the answer must
account for, grouped by how deeply each must be read:

| depth | a document | a table |
|---|---|---|
| `overall` | what it covers as a whole | what it is about and what it holds |
| `parts` | what its individual relevant sections cover | — |
| `full` | the wording of those sections | values drawn or computed from its rows |

Two properties follow, and they are the point of the design. **Coverage is a set we can enforce**, not a
length we infer from how much some stage happened to return. And **the two kinds of evidence are judged
together**: a question about the collection can put a table in coverage at `overall` and a document at
`parts` in the same decision, which two independently-run channels could never agree on.

Only what is listed reaches the answer, so the prompt says exactly that — nothing downstream sees the
inventory, and an item left out contributes nothing however plainly it was described.

**Tables that need their rows.** Only those go to the query writer, and they arrive with full schema and
sample rows — the expensive view, bought once the set is small. The sample rows show how values are
*encoded* (`2022-Q2` versus `Q2 2022`, `"007"` as a string); a predicate written against a schema alone
silently matches nothing. The row count is carried at both steps, and it does two jobs: at resolution it is
how big a table is, which questions ask about directly; at writing it is what makes the model aggregate a
large table rather than ask for raw rows.

The writer no longer states relevance — it only writes queries, because every table it sees was already
resolved as needing its rows. A table that matters for *what it is* never reaches it, and is carried by its
description instead. There is no synthetic relation over the corpus's own shape: the row counts and
filenames are in the inventory the resolver reads, and a relation supplied for questions *about* the tables
turned out to teach the model to project filenames whenever a question sounded corpus-shaped.

### Why the wide net was removed

Similarity deciding *membership* over the whole corpus is what made broad questions pathological. "What are
these documents" matches everything, so a corpus-wide net sweeps every passage in order to filter nothing.
Under resolution the same question puts every document in coverage and lets depth be decided afterwards —
the widest question becomes the *cheapest* path, not the most expensive.

Similarity is gone from the query path entirely. A document is carried at its summary, at the summaries of
its parts, or at their wording; there is no ranking or filtering *within* a document to deepen it
selectively. Structure alone decides membership and coverage. (Within-document similarity retrieval is
parked, not load-bearing: it would recover page-level precision inside a document shown at block depth, and
can return without changing the fitting rule below.)

### Fitting — floor first, then rows, then depth

Because resolution was asked for what the answer must **account for**, everything it returns must appear in
the answer. Fitting therefore adjusts *depth*, never membership. The rule is deterministic — no
cross-channel model call, no round-robin — and it runs in one order for both kinds of evidence.

**The floor is reserved first.** Every covered table's description and every covered document's summary are
priced before anything else is fitted. This is what makes coverage a property of the output rather than an
outcome of the budget: no item can be crowded out by another item's volume, because the cheapest faithful
form of every item is paid for before the first expensive thing is bought. If even the floor overflows, its
summaries are merged — at document grain first — and never dropped.

**Computed rows take what the floor leaves.** Results are exact and already minimal (the query produced
exactly the rows asked for), and `_fit_results` keeps *every* result table, reducing rows round-robin across
them only if they collectively overflow, with an explicit "showing N of M rows" marker. Nothing is
re-aggregated after execution: the query already decided the shape. The genuine prevention is upstream — the
writer sees each row count and aggregates in SQL when a raw `SELECT *` would be huge. Rows come *after* the
floor because a result is an answer, and an answer may not crowd out what has to be accounted for.

**Depth takes what the rows leave, capped at an equal share per document.** Each covered document is carried
one rung deeper while its own share of the remaining budget allows: the depths resolution asked for have
first claim, and whatever is still spare carries the rest of the coverage from its summary to the summaries
of its parts — an unspent budget buys nothing. Each ask carries its own way down, so a document whose
wording will not fit lands on its parts rather than falling back to a one-line summary. Both rungs are
priced from what ingestion stored — `content_tokens` for wording, summary tokens for parts — so an
unaffordable rung is rejected before anything is read.

The **equal share** is what keeps the answer proportionate. A long document's parts outnumber a short one's
many times over, and without a ceiling the longest document in the coverage writes most of the answer: a
825-page volume contributed ~25 summaries beside a one-page invoice's one, and the answer read as being
mostly about that volume. No document may take more than `remaining / covered documents`, and one that
cannot fit its share stays at its summary.

So depth is **measured, never classified**: real sizes against a real budget decide each rung, not a count
or a score. Membership is never cut — a document that cannot be shown at the depth it asked for is shown at
a shallower one, never dropped — and because every rung cites at page level or better, coverage is honest at
any depth.

### Arithmetic stays in the database

A `SUM` is computed by SQL, never by a model reading rows. The result tables are the answer; they are never
reduced by re-aggregation. A join's cardinality is not predictable from its inputs, so a near-cartesian
blowup is discoverable only after execution and is usually a wrong key — worth logging as a correctness
signal, not just a budget event. Whenever rows are dropped to fit, it is marked, so the answer says "the top
50 of 500" rather than presenting a partial as a total.

A single computed value carries no evidence of its own scope: `17582.254` under a model-chosen name reads the
same whether it is a grand total or filtered to one segment and ship mode, and synthesis, seeing only the
number, will disclaim or misattribute it. So each result is rendered with the **operation that produced it** —
the executed query with its column references rewritten to header names, no internal position ids. This is
provenance for synthesis to read, not for the reader to see: it states what a figure means so a filtered
figure is never mistaken for the whole, and synthesis is told to state the figure plainly and never surface
the operation.

### Citation is structural and free

Citations do not depend on depth. A batch shown at summary depth still cites at block and page level, because
the batch stored its range when it was built; a block shown directly carries its own filename and page; a
document shown at its own summary cites the file. This is what makes shallow depth honest: a document too
broad to expand still contributes its real provenance.

What citation cannot check is *which rung a figure came from*. A number quoted out of a summary and a number
computed from rows cite identically, and the fabrication check — which compares cited files against evidence
files — passes both. A figure lifted from prose that happens to be wrong is therefore invisible to it.

### Synthesize

One model call merges the text evidence and the computed results. Computed results are the authority for
figures and totals; text supplies narrative and context; the model is where the two reconcile. If the
evidence does not answer the question, it says so plainly. It is told to account for each piece of evidence
once: many near-identical summaries from one long document are the condition under which a single free-running
call degenerates into repeating itself until it hits its token cap.

### The one irreducible risk

Summary quality. Two summaries now stand between the query and the content, and they fail differently. A bad
**batch** summary misrepresents a section. A bad **document** summary is worse: it is the only thing
resolution ever sees of that document, so a document whose summary omits what it holds is not merely
under-read — it is never selected at all, and the failure is indistinguishable in the log from a document
that genuinely bears on nothing. Both are set once, in the prep phase, and neither can be compensated
downstream.

### Open

- **Corpus scale.** The inventory grows with the corpus — one summary per document, one entry per table —
  and resolution is deliberately **one** call, because splitting it would fragment the judgment it exists to
  make: each call would weigh how much of the collection the answer must account for against the slice it was
  given, never the whole, so the same question would be judged against a different denominator in every call
  and the union of their choices would be bounded by nothing. A per-item ceiling (a fixed document-summary
  length; identity and column names, never values, per table) keeps the inventory small, but the total is
  still linear in items — 136 items measured at ~16k tokens — so past a few thousand, scoping needs its own
  retrieval step ahead of resolution. The same ceiling appears at synthesis, where a floor that overflows is
  merged. One library fits one call today, so this is deferred, not solved. Table entries carry a second
  term: their cost is linear in total *columns*, not tables, so a corpus of wide sheets reaches the ceiling
  sooner than a corpus of many narrow ones.

- **Resolution is document-grain, so within-document specificity is invisible to it.** Only document
  summaries are read when coverage is decided, so something named in exactly one section of one document —
  and not in that document's summary — cannot be routed to. The false negative is indistinguishable from a
  question the corpus genuinely has nothing on: both log as empty coverage.

### The pipeline this replaced

The first pipeline reformulated the question, retrieved a wide net (`CANDIDATES=1000`, RRF over dense and
lexical channels), filtered it with a batched two-step model pass, wrote SQL for the kept tables, marked
evidence essential or supporting, and fit deterministically. Its properties were sound in isolation — no
score threshold, no reranker, no arbitrary read cap — but it rested on similarity deciding *membership*,
which is the assumption that fails on broad questions. The old RRF net and early-stopping filter are retired
from the query path, kept only as the parked within-document retrieval that could later deepen a
block-depth document.

It was replaced by two independent channels — a per-batch selection pass over every batch summary, and a
table pass that both marked relevant tables and wrote the SQL. That failed differently, and structurally:

- **Selection was a hard gate with a silent failure mode.** An empty return was indistinguishable from
  "nothing here is relevant", and on a question spanning the whole corpus it returned nothing at all, so the
  answer was written from the table channel alone with 96% of the budget unspent.
- **Nothing decided between the channels.** They ran concurrently and never saw each other, so precedence
  was fixed in code — computed rows claimed the budget first and text was the residual — regardless of what
  the question needed.
- **Relevance stated by the SQL writer taught the wrong lesson.** Its prompt's worked examples paired a
  question *shape* with an answer, so a corpus-shaped question reliably produced a projection of filenames,
  and a prose-sounding one reliably produced no tables at all. A synthetic `catalog(file, sheet, row_count)`
  relation, added so questions *about* the tables could be a real `SELECT`, was what that projection read
  from — it was removed along with the examples, because the same facts are already in the inventory
  resolution reads.

## Models

One general vision-language model (Qwen, served by vLLM), run without a separate reasoning pass, performs
every model step across ingestion and query — reading rendered pages and embedded images, table structuring,
batch summarization, document summarization, query resolution, SQL writing, evidence merging, and synthesis.
There is no separate layout/detection model and no separate text-only model: the same model that reads a
page's pixels also structures tables and writes SQL.

A multilingual dense representation (`BAAI/bge-m3`) carries meaning for search. It is served the same way as
Qwen — its own vLLM instance, behind an OpenAI-compatible `/v1/embeddings` endpoint — rather than loaded
in-process; every process that needs an embedding is a stateless HTTP client to that one service, gated by
`get_embed_capacity()`, the same shared-service-plus-global-pool pattern used for every other model call. It
used to be loaded directly into whichever process called it (`sentence-transformers`, on-device), which meant
every replica of that process held its own full copy of the model in GPU memory with no cross-process
coordination; serving it removes both problems at once. Nothing in the pipeline asks a model to hold data in
its head: it reads, judges, and writes queries; the database keeps the numbers.

---

## Known limitations

Open gaps in the current build. None corrupts an answer — each is a place the system is weaker than the
design intends.

- **Missed headers and section-labels on a rendered page (open — the mechanism that would fix this across
  pages does not apply to this lane).** A *genuinely* header-less table correctly gets generic `col0…colN`
  columns — a key-value block, a bare listing — and its rows are faithful, just unnamed; that is the right
  structure, not a defect. A header row that *is* present but a continuation page loses, or a section-label
  row flattened into a data row, is a fidelity question that today rests entirely on the single vision-model
  call that read the page — there is no downstream candidate-merging stage for PDF/image tables that could
  catch it after the fact (that mechanism — several candidates shown to the model at once, so it can mark
  section rows and merge a header-once table's continuation back under its header block — exists only for a
  spreadsheet's raw cell range; see "Two structuring mechanisms" above). A model that reads a continuation
  page in isolation, with no visibility into the header on the page before it, has no way to recover it after
  the fact under the current architecture. Confirmed as a real fidelity gap, though a different instance of
  it than the one first described here: an OCR pass was observed dropping a table's own section-header rows
  entirely (a Malaysian court-circular's `MAHKAMAH TINGGI`/`MAHKAMAH SESYEN` case-code table) — a larger vision
  model correctly kept both on a re-run of the same page, but this was one comparison, not a systematic
  re-check, and the transfer-credit-report/continuation-page scenario this bullet originally described has
  not been specifically re-verified.

- **Verified fix (2026-08-10): prose surviving as a table cell.** The case this bullet used to describe —
  running prose gridded into a table with a whole paragraph stuffed into one cell — was reproduced live on
  two real, unrelated pages: a Malaysian statute's table of contents (plain numbered section list) and a
  Kazakhstan immigration-procedure numbered list, both forced into a mostly-empty table by the OCR prompt's
  earlier wording, which described what a table should look like but said nothing about lists or code as
  the alternative. Naming that alternative explicitly — a list stays a markdown list, code stays fenced —
  and checked against the same two pages: both now come back as headings/lists, not tables. Not exhaustively
  re-checked across every document shape, but the specific previously-observed failure is closed.

- **Verified fix (2026-08-10), root cause not fully isolated: fabrication under concurrent load.** A page
  dense with real legal text, sent through OCR alone, was transcribed correctly every time. The same exact
  page, sent as part of a larger concurrent batch, twice came back as a table of roughly seventy blank rows —
  a fabrication, not a misread, on content the model handled correctly moments earlier under no load.
  Re-checked after the prompt rewrite above, at the same concurrency, against the same page: the fabrication
  did not reproduce. Whether the prompt change is what fixed it, or whether it was closely coupled to a
  vision-concurrency setting also being tuned in the same window, was not disentangled — flagging this as
  fixed against the configuration actually shipped, not as a mechanism fully understood. Separately: genuinely
  blank pages showed the same failure shape (fabricated content instead of empty output) and are now closed
  structurally by the blank-page pixel-variance guard described above, which is a different, load-independent
  fix from whatever closed this one.

- **Stray query on a prose question.** A purely narrative question ("what is this dispute about")
  sometimes still draws a single read-only query against a loosely related table. It is harmless — the
  query is single-table and its result is dropped at unify when it turns out not to answer the question —
  but it spends a model call and a database round-trip it did not need.

- **A client-side elapsed-time reading on an OCR/model call conflates queueing with work.** The inference
  server admits requests by its own scheduler, independent of how many the client has dispatched, so a
  request that has been sent can sit inside the server's queue for most of its measured duration before any
  compute actually starts on it. A backlog of requests waiting inside the server is not starvation — the GPU
  stays saturated the whole time — but any duration timed from dispatch to response is dominated by that
  wait, not by inference. Getting the real split requires the server's own per-request queue/inference
  timing, not a client-side stopwatch around the call.

- **Concurrency and host-memory behavior are not yet re-measured against the current (whole-page,
  single-model) pipeline shape.** The admission model — a shared budget for page rasterization and
  embedded-image resolution, a separate cap on how many requests are actually dispatched to the model, and a
  further ceiling inside the inference server's own scheduler that the client does not control — is current
  and described above. Concrete throughput and peak-memory numbers for it are actively being established,
  not carried forward from the retired crop-based pipeline's figures.

- **Run-to-run variance exists and has not been re-quantified for this pipeline.** The previous pipeline
  measured a few percent variance between identical runs; whether that holds here has not been re-checked.
  Treat any single-run comparison as provisional until it has.

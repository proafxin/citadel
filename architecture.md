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
| **Visual** | PDF, images; slide decks and legacy Word (converted to PDF) | content tree |
| **Tabular** | spreadsheets; CSV/TSV; JSON | canonical Table + rows |

Plain text is a degenerate markup case: split on blank lines into paragraphs.

### What each format becomes

| Source | Lane | How |
|---|---|---|
| spreadsheets (XLSX / XLS / ODS) | tabular | cells read directly |
| CSV / TSV / JSON | tabular | typed parse / structural shred |
| HTML | markup | parsed as-is |
| Markdown | markup | converted to HTML (math kept as LaTeX) |
| Word / ODT / RTF / EPUB | markup | converted to HTML |
| PowerPoint / ODP / legacy Word | visual | converted to PDF |
| PDF | visual | pages rendered; each marked *digital* (has a text layer) or *scanned* |
| images | visual | read as one page |

## Pipeline

Ingestion runs as streaming workers on a bus — an upload feeds an always-running pipeline, not a
per-upload script. Each phase is an independent, asynchronous consumer running concurrently, and work is
**page-granular**: the unit of work is a page, not a document, so the whole corpus reads in parallel. Every
stage is a FIFO-fed sliding window — it claims only as much as it can hold, and a slot frees the moment one
item completes, never in batches. The phases:

```
upload → normalize → paginate → render → ocr → structure ─┬──────────────────────→ merge → relational store
                                                          └→ table_structure ────→ merge
                     └───────────────────────────────────────→ table_structure ────→ merge
```

| Phase | Work |
|---|---|
| normalize | route + convert to one of the three lanes |
| paginate | count PDF pages and emit one render job each; or split markup/text/tabular into units |
| render | rasterize one PDF page |
| ocr | detect the page's regions, then read each one → blocks |
| structure | a document's blocks → paratext split, reclassify, stitch tables; then route — no tables goes straight to merge, each table found becomes its own job |
| table_structure | one job per unit — a document's whole set of candidate table blocks, or a spreadsheet sheet — → canonical Tables; the model sees every candidate at once and in one call structures each real table, drops the spurious ones, and merges those that are one table split apart |
| merge | assemble the content tree from the prepared blocks and the finished tables → persist |

**Table structuring is one job per unit, and every unit is the same kind of work item.** Whatever the source —
a spreadsheet sheet, or all the tables a scanned book yielded — its candidate tables go to one job on one
stream, claimed the same way as everything else. The job hands the model every candidate at once (each shown
as bounded sample rows, never full data), and the model makes every call in that single pass: which candidates
are real tables, each one's structure, and which candidates are one table split across pages or regions and
should merge. Deterministic code decides nothing about structure — it only gathers the candidates and builds
what the model returned, placing each table under its first source block. The table job fires merge when it
finishes, so redelivery is a no-op and merge runs exactly once.

A document with no tables never enters that stream at all: it merges as soon as its pages are read, so it is
structured and stored *during* the run rather than waiting behind work it does not have. (Becoming
*searchable* is a later, separate step — the prep phase in Part I's finalize — not part of this pipeline.)

The bus carries only lightweight in-flight state — page images and per-page blocks; the uploaded source is
held once in a doc-keyed store and memory-mapped by each stage that reads it, never copied through the bus
per page.

### What bounds the pipeline

A page is read in two steps, and they are two different kinds of computation:

- **Detect** — one forward pass over the whole page. It returns boxes, classes and a reading order, and
  nothing else. It is a *detector*, not a generation: it emits no tokens, so its cost does not grow with how
  dense the page is, and it cannot loop, hallucinate, or return an empty page. It runs in the worker's own
  process pool, off the inference server entirely, at **70–90 ms per page** — a rounding error beside the
  reading that follows.
- **Read** — one request per region, on a crop of it, with the task chosen by what the detector says the
  region *is*: prose, table, formula, chart, or seal. A scanned page yields tens of these; a born-digital one
  yields only a few, because its text comes from the page's own text layer and is never sent to the model at
  all.

This split is the whole reason the pipeline is fast. Layout used to be a *generation* — the model wrote out
every block it found, which made one request per page cost more than all of that page's crops combined, and
made throughput a function of page density. As a detector it is not on the critical path at all.

So a page is worth wildly different amounts of work depending on what it is, and **no page count can be
right for both**: set it for scanned pages and the model starves on digital ones; set it for digital pages
and a scanned document floods it. Concurrency is therefore expressed in **requests**, not pages — one
semaphore caps what is outstanding at the model, and everything else exists only to cap *memory*.

What is left is **prefill-bound**. A crop's request is roughly 200 image tokens in and 70 tokens out, so the
GPU spends most of its time reading pixels, not writing text. The remaining levers are therefore about
sending the model *less*, not about feeding it *more*: it is already saturated.

Two rules follow, and both were learned the hard way.

**Nothing between that semaphore and the model may bound lower than it does.** The HTTP client pool is the
trap: a request that cannot get a socket blocks *inside* the client, holding its permit and sending nothing —
so the model starves with nothing in our own logs to say so. Sockets are therefore **derived** from the
semaphore with several times the per-client mean as headroom, never chosen as a literal. The semaphore in
turn equals the server's own sequence limit: below it the GPU cannot fill its slots; above it the excess
merely queues *inside* the model, where every waiting request pins a decoded image.

**Nothing that holds the interpreter may sit on the request path.** The detector is CPU-bound Python, and
run as in-process threads it held the GIL and starved the event loop driving every crop request in flight —
the GPU stuttered and pages waited seconds on a call that takes 80 ms. It runs in a **separate process** for
that reason alone, and exactly one: two detector processes contend for the card and measure *slower* than
one, while each carries its own CUDA context.

**A memory bound must never become the throughput constraint.** Each is sized so the request semaphore runs
out first. Get either rule wrong and the symptom is identical to a slow GPU: the model idles, every knob
looks innocent, and nothing in our own logs says otherwise.

**Bound what holds memory; never bound what the server already schedules.** The two model workloads are
opposite in shape and take opposite treatment. A *crop* is capped by us, because an outstanding crop pins a
decoded region in our own memory — the bound is about RAM, and it happens to equal the server's sequence
limit. A *text request* holds nothing on our side, so every cap we place on it can only subtract: the
inference server admits by free cache, which is size-aware in the way a request count can never be, and a
request that does not fit simply waits, holding token ids and no cache at all. Table work is therefore
dispatched **unbounded** and the server decides what runs.

This was three separate caps before it was one rule, and their product was invisible: a stage cap derived
from the CPU count (three, on a 24-core machine), a semaphore in the dispatcher, and the server's own
sequence limit. Each looked defensible alone. Together they held the model at three concurrent requests and
6% of its cache for an entire run, with *nothing waiting* behind them — the exact signature of a saturated
GPU, and the reason it went unnoticed. A count-based cap cannot serve a table description of a thousand
tokens and an evidence batch of thirty thousand at the same time; free cache serves both without being told
which is which.

Which is why the stage line separates **waiting** from **working** — every span is one or the other, never
both. A span that mixes them cannot answer the only question worth asking. And no span is ever reported as a
raw sum: pages run concurrently, and so do the crops inside a page, so every accumulator is a sum of
*overlapping* intervals, not a duration. Each is divided by the units it was summed over — per page, or per
crop — and reported as a mean, with the crop count printed beside it so the denominator is never in doubt.
A sum of overlapping spans is not a number that means anything.

Memory then follows from what each thing is needed *for*, not how long it is referenced:

- A **decoded bitmap** is needed to lay out and to cut — never to wait on the model, which is the long
  part. It is built, used, and dropped inside a bounded gate; a page waits out its requests holding only
  its (far smaller) encoded bytes.
- A **crop** is compressed the moment it is cut, so what is held during the wait is the wire format, not
  raw pixels.
- **Pages claimed but not yet charged** are bounded, and only those. Between the claim and the detector
  finding a page's regions, a page holds an encoded image that no other bound can see — that window is the
  one thing a page cap must close. Past it the page is fully represented in crops and counting it again is
  a mistake with a cost, described next.

**Admission is denominated in crops, never in pages.** A page's crop yield spans **0 to 23.5** across real
documents — a scanned book emits ~18 crops a page, a born-digital one 0.8, and a page whose text comes
entirely from the text layer emits none at all. So any fixed page count is simultaneously too loose for one
document and too tight for another: at 96 pages, a 0.82-crop/page book could place only ~79 crops in front
of 128 slots and could claim no more, because its pages were still holding slots while their crops queued.
The model idled with every queue reading zero — the failure that looks exactly like a fast GPU. Admitting
until the *crop* supply is satisfied fixes it without anyone deciding which kind of document is arriving:
a dense page fills the budget in eight pages, a sparse one draws hundreds, an empty one costs nothing.

Host memory is therefore **flat** — a function of these bounds alone, not of how many documents are
uploaded, how large they are, how dense their pages are, or how long the queue gets. Backpressure
propagates the whole way back: requests fill → pages stop being admitted → rendered pages back up → render
stalls → paginate stalls. Measured: **~18 GB peak host RAM** across a 35-document run, with other
applications on the machine, and the same figure whether the corpus is a page or a thousand.

The number that decides whether any of this is working is **occupancy**: total model work divided by what
the GPU could have done in the elapsed time — `Σ(crops × predict)` against `runtime × concurrency`. It
separates the two failures that look identical from the outside. The gap below 100% is a *packing* problem,
fixable by admitting differently; the remainder is a *work* problem, fixable only by sending fewer or
cheaper crops. Crop-denominated admission moved it from **78.9% to 84.4%**; proportional-share admission
(below) took it to **90.6%**, and the run from 537.5s to **454.6s**, on an unchanged corpus.

**Admission is shared between documents in proportion to the work each still owes.** A document's remaining
crops are its observed density — crops read divided by pages finished — times the pages it has left, so the
share is computed per run from the corpus at hand and nothing here is fitted to a particular library. All
three orderings were measured, and the two obvious ones are both wrong:

| ordering | runtime | why |
|---|---|---|
| FIFO | 472.8s | the dominant document's jobs sit behind everyone's, so crop-sparse documents run alone and the model collapses to 1-5 crops in flight for 70 seconds |
| round-robin | 478.0s | equal share gives the critical path 1/N — about 5% — which fixes the stall and stretches that document 27% |
| proportional | **454.6s** | every document finishes at once, which is where makespan is minimised |

Equal share is therefore never the fallback, including on the first pass before any density exists — there
the weight comes from page count, which already ranks the dominant document first. And because a pass offers
only a handful of slots, a document owed one percent of it would round to zero and never be served at all;
shares accumulate as credit across passes so a small share is paid late rather than never. The density is
floored at one crop per page, because a fully-digital document owes no crops — its text comes from the
layer — and weighting on crops alone would leave it unrendered forever.

The queues that then appear are the confirmation, not a regression. `budget_wait` became non-zero for the
first time in any run (29.5 s/pg on one document), which is the crop budget finally binding — crops are
abundant and the model is fed. `layout_wait` rose to ~52 s/pg, which is precisely the admission window full
(512 uncharged pages at ~100 ms a detect = 51.2s). `crop_wait` reads 109–220 s/crop on the dense books, and
that is a queue *depth* readout, never a cost: driving it down without cutting work only moves the queue
upstream. A saturated consumer is supposed to have a deep queue in front of it.

What remains is arithmetic, and scheduling is now spent. At 90.6% the idle is **41s**, of which 3s is the
ramp before the first crop exists and 38s is the drain — the last document's last pages, which no ordering
can fill. Everything below that requires cutting the work itself, and once page furniture stopped being
re-recognized (below) the pipeline landed exactly on its throughput floor:

```
crops 16,933 ÷ 39.5 crops/s = 428.7s        actual runtime 428.5s
```

**Runtime is now the crop count divided by a fixed GPU rate.** Three concurrency sweeps put that rate at
39.5 crops/s and it does not move, so every further gain has to remove crops. Two scanned books are **92.6%
of them** — the corpus is, for optimisation purposes, those two documents.

### Reading pages (ocr)

The detector runs on every page and produces the same thing regardless: regions, their classes, and the order
a human would read them in. What happens to each region then depends on whether its characters already exist,
because re-recognizing text that is already present only introduces errors:

- **Digital page** (real text layer) — prose regions AND page furniture are taken from the page's own text
  layer at each region's box, so they are the document's own characters, never re-recognized, and never sent
  to the model. Headers, footers and page numbers belong here for the same reason the body does: the layer
  holds them exactly, and cropping them cost ~1.5 crops a page to get a worse answer.
  Only what the text layer cannot supply is cropped and read. Blank and fill-in fields are re-read from a
  close crop to catch ink the text layer lacks.
- **Scanned page** — every readable region is cropped and read. There is no second pass: the detector finds
  the regions and each one is read once. A generation-based layout could silently drop a whole page's blocks
  and needed a gap-filling pass behind it to recover them; a detector cannot, so that pass no longer exists.
- **By region type** — a table is read as a grid, a formula as LaTeX, a chart and a seal each as their own
  task. Photographs and figures are never sent: they have no text to recognize. An inline formula is not read
  separately, because it lies *inside* a text region whose read already returns it inline.

Tables come back in a token-efficient grid notation — one token per cell rather than styled markup — and are
translated to HTML at the model boundary, so a dense table costs a fraction of the tokens and everything
downstream still sees the one grid format it was written against.

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

A second rule decides *when* a model is asked at all: **the model is asked only where the source is
genuinely ambiguous.** Almost every format states its own structure. A `<table>` marks its header with
`<thead>` or `<th>`; the recognition model marks a header row in its own output; a CSV's first row names
its columns; a JSON entity's keys *are* its schema. In all of those the header is read, not inferred, and
no model call is made. Only a **spreadsheet's raw cell grid** is genuinely undecidable — tables can begin
anywhere on a sheet, several can share one, headers can span rows, and nothing in the file says which cells
are which. That is the one place structure is asked for.

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
- **Embedded HTML/PDF tables** — the grid is built deterministically with spans expanded, and the header
  band is read from the markup: `<thead>` or a leading run of `<th>` for documents, and for a scanned page
  the recognition model's own header marking, which it emits alongside the cells and which is carried
  through into the HTML rather than discarded. No structure call. Tables continuing across consecutive
  pages are stitched into one first — fragments with a matching column count across a page boundary merged,
  repeated headers dropped, page furniture skipped — so a forty-page table is one table.

Where a model *is* asked, it is shown a bounded view and never the table: at most **20 rows** — the top few,
the bottom few, and every Mth in between, with M widened as the sheet grows so the sample spans its whole
height — against **every column**, because the columns are the schema and a header dropped is a schema lost.
A payload is therefore a function of a table's width, never of its length: a five-row sheet and a
five-million-row sheet cost the same call. Sizing that view by a *token budget* instead is a mistake made
and measured — it let a prompt grow to fill whatever window was available, producing 20k-token requests to
decide which rows were headers.

A document's candidate tables pass a **validation** step before they are kept. Structure detection
over-produces — a block of prose gridded into cells, a figure or a caption read as a one-row table, a
region that is not tabular at all — so an LLM is shown the candidates for a document and judges which are
real tables. It is a per-candidate keep/drop decision, batched across the document's candidates (a set too
large for one call splits losslessly, each batch deciding its own members), so a spurious grid is dropped
before it ever reaches the query side. The model decides *membership*, never content — the same boundary as
structure. This is what reconciles over-detection: a document whose structure pass proposed nineteen
candidates keeps the six that are genuinely tables.

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
once both land — the summary work is on the SLM, the embedding on the embedder, and running them together
hides the shorter (embedding) under the longer (summaries) instead of stacking the two. Summaries used to
stream per document *during* ingestion, overlapping OCR; that overlap paid a GPU-contention tax and, worse,
let a query in the window resolve over a partial inventory. Running them in this phase, gated on the
library's last document draining, closes both, and the summary wait is event-driven — the last summary to
land fires a completion signal — never a poll.

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

One general model, run without a separate reasoning pass, performs every model step across ingestion and
query — table structuring, batch summarization, document summarization, query resolution, SQL writing,
evidence merging, and synthesis.

Reading a document that exists only as pixels takes **two** models, deliberately split:

- a **layout detector**, which finds the regions and their reading order in a single forward pass. It emits
  no tokens, so it is cheap, bounded, and structurally incapable of the failure a generation has — inventing
  a region, looping, or returning an empty page.
- a **recognition model**, which is shown one region at a time and asked only to read it. It never sees a
  whole page, so it is never asked to decide what a page *is* while also reading it.

Splitting them is what makes the throughput possible, and it is also what makes the layout trustworthy: the
two questions have different failure modes and no longer share an answer.

A multilingual dense representation carries meaning for search. Nothing in the pipeline asks a model to hold
data in its head: it reads, judges, and writes queries; the database keeps the numbers.

---

## Known limitations

Open gaps in the current build. None corrupts an answer — each is a place the system is weaker than the
design intends.

- **Missed headers and section-labels in irregular PDFs (targeted, unverified).** A *genuinely* header-less
  table correctly gets generic `col0…colN` columns — a key-value block, a bare listing — and its rows are
  faithful, just unnamed; that is the right structure, not a defect. Two narrower failures used to slip
  through the old per-block path: a header row that *is* present left as a data row (a transfer-credit report
  whose `Transfer Term | Incoming Course | …` header sat in the data, because the continuation page was
  structured in isolation with no header), and a section-label row *inside* a table (`1B. ENG 121`) flattened
  into a data row. The unified stage is built to close both — the model sees the whole document's candidates
  at once, so it marks section rows and a header-once table's continuation merges back under its header block
  — but this has not yet been confirmed on a real run.

- **Prose occasionally survives as a table cell.** Validation drops most non-tabular candidates, but a page
  of running text laid out in a way that reads as a two-column grid can still slip through with a whole
  paragraph stuffed into one "cell" (observed on a block of statute text). Grid-shaped enough to pass, it is
  a poor representation of what was never a table — harmless unless a question turns on it.

- **Stray query on a prose question.** A purely narrative question ("what is this dispute about")
  sometimes still draws a single read-only query against a loosely related table. It is harmless — the
  query is single-table and its result is dropped at unify when it turns out not to answer the question —
  but it spends a model call and a database round-trip it did not need.

- **Filter breadth on broad questions.** The relevance filter is tuned to be precise, which serves pointed
  questions well but can keep too little for an open "summarize everything about X": the answer is correct
  but thinner than the corpus could support. This is a prompt-tuning axis, not a structural limit.

- **Throughput is bounded by the GPU's power limit, and the remaining levers are small.** The card runs
  pinned at its cap — 142 W of 145 W, clocks sagging from 2205 to 1867 MHz as it heats — at 93%
  utilization. It is saturated *while it has work*, and the qualifier is the correction: an earlier version
  of this note claimed adding page supply anywhere "does nothing," which was measured on dense documents
  and wrongly generalized. A crop-sparse document could not put 128 crops in front of the model at all
  under a fixed page cap, and the model idled while every queue read zero. Crop-denominated admission
  recovered 5.5 points of occupancy, and proportional-share admission another 6. Occupancy now sits at
  **90.6%**, and the 41s left is 3s of ramp before the first crop exists and 38s of drain — the last
  document's last pages, which no ordering can fill. Scheduling is spent; everything past this requires
  sending the model fewer crops (below), not feeding it faster.

- **Every crop pays a fixed token toll, and most crops are a fraction of what that toll buys.** The
  recognition model's own image processor smart-resizes each crop into `[112896, 1003520]` pixels — the
  same window our own resize uses — at `patch_size 14`, `merge_size 2`, so one token covers 28×28 pixels
  and the floor is **144 tokens, charged whether the region fills it or not**. Measured over a full run:
  **95.5% of 21,607 crops fall below that floor**, 52.8% below an eighth of it, and **74.1% of the entire
  token bill is padding** rather than content.

  Lowering our own `MIN_PIXELS` does **not** recover it, and that is a closed question: the model re-floors
  anything smaller itself, so removing our floor entirely moved the bill from 193.3 to 196.0 tokens/crop —
  *up*. Our resize duplicates the model's and costs only CPU and bandwidth. It is also not a legibility
  tradeoff, for the same reason.

  What the padding measures is **packing headroom**. Eight regions that each cost 144 tokens alone still
  cost 144 tokens merged, so the lever is crop *count*, never crop size. Merging adjacent same-label prose
  took 21,607 crops to 18,652, and that seam is now **exhausted**: instrumented over a full run, merges are
  blocked by a **label change 81.1%** of the time and by the crop running out of room only 18.7%. The next
  region is a formula, a heading, a list — not more prose. So composing crops more tightly recovers almost
  nothing, and the remaining ~68% is reachable only by merging **non-adjacent** same-label regions, which
  requires re-associating one returned text with several source regions. A probe settled that: composing
  four regions into one crop cost 25% of the tokens and returned **byte-identical output whether or not a
  separator was drawn between them** — the model transcribes the image and ignores rules and index marks
  alike, so there is no boundary to split on. Non-adjacent merging is therefore closed, not merely hard.

- **A merge may grow a block downward, never sideways — and area cannot express that.** The first version of
  region packing tested only whether the union still fitted the token floor. Two regions **side by side**
  have a *small* union area, so two columns merged happily into one wide strip: measured on a bilingual
  letterhead, the Kazakh and Russian addresses became one 89%-wide crop and the model returned `данфылы`
  where the scan reads `даңғылы`, which the reference pipeline read correctly by keeping the columns apart.
  The fix is a test on **width**: a merge must not widen the block beyond the wider of its two parts. It
  caught **887 fused pairs in a single multi-column book**, whose agreement with an independent read of the
  same pages rose 0.935 → 0.968 while documents with no fusions moved within noise, and it cost no runtime.

  The lesson generalises past this bug. The claim "packing is harmless" had been made twice off aggregates —
  once from the model never queueing, once from 0.99 corpus-wide agreement — and both times the failure was
  real but confined to a minority of blocks, which a mean cannot show. The 0.935-vs-0.990 gap *was* the
  signal. It took rendering the actual scanned page to produce a mechanism to attach to it.

- **Recognition requests are dropped intermittently, and the cause is not established.** About one crop in
  sixteen thousand fails with the server closing the connection without a response; the server logs nothing
  and continues serving a full batch through the failure. It reproduced once in a run and not at all in the
  next, at identical settings. No fix has been shipped for it, because no cause has been proved — several
  plausible ones (keep-alive expiry, socket-pool exhaustion, event-loop starvation) were each tested and
  each disproved. The server's access log is now enabled so the next occurrence distinguishes "the request
  reached the application and was dropped" from "it died beneath it," which are different bugs.

- **Small documents still finish late, and that is now a deliberate trade rather than a defect.** Admission
  shares each pass in proportion to the work a document still owes, which is what minimises makespan — every
  document finishing at once. A 13-page file therefore trickles through beside an 809-page book rather than
  being rushed ahead of it. Fair-share admission would make small files finish sooner and the *run* finish
  later; it was built, measured at 478.0s against 454.6s, and reverted. If per-document latency ever matters
  more than total runtime, that is a policy choice with a known price, not a missing feature.

- **A memory bound that becomes the throughput constraint is invisible from the outside.** The failure
  looks identical to a slow GPU: the model idles, the queue drains, and every knob looks innocent. It is
  only distinguishable by asking where a page's time actually went — which is why every span in the stage
  line is either a wait or a work, never both. Tuning these by inference rather than measurement reliably
  picks the wrong one.

- **Run-to-run variance is a few percent.** Identical configurations have measured 426.4s and 426.8s on the
  current build, and drifted several percent on the previous one. Any single-run comparison inside that band
  is noise, and treating it as a result is how most of a day gets spent tuning constants that never mattered.

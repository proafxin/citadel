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
upload → normalize → paginate → render → ocr ─────────────────→ merge → relational store
                                           └─ gapfill (scanned) ──┘
                     └──────────────────→ tabular ──────────────→ merge
```

| Phase | Work |
|---|---|
| normalize | route + convert to one of the three lanes |
| paginate | count PDF pages and emit one render job each; or split markup/text/tabular into units |
| render | rasterize one PDF page |
| ocr | read each page visually → blocks |
| gapfill | scanned pages only: recover the lines the visual read dropped |
| merge | blocks → split paratext → stitch tables → structure embedded tables → content tree → persist |
| tabular | per sheet/file: structure → canonical Table (its retrieval description is written later, at finalize) |

The bus carries only lightweight in-flight state — page images and per-page blocks; the uploaded source is
held once in a doc-keyed store and memory-mapped by each stage that reads it, never copied through the bus
per page.

### What bounds the pipeline

The vision model is asked two different things per page, and they cost very different amounts:

- **Layout** — one call on the whole page. It returns nothing but boxes, yet it is by far the most
  expensive request we make: the model *writes out* every block it finds, so a dense page (hundreds of
  blocks) is a long generation. Measured against true model time — queue wait excluded — layout is
  **84–89% of all GPU work** on dense documents, from one request per page against dozens of crops.
- **Reading** — one call per block, on a crop of it. A scanned page yields tens of these; a born-digital
  one yields only a couple, because its text comes from the page's own text layer and is never sent to the
  model at all.

So a page is worth wildly different amounts of work depending on what it is, and **no page count can be
right for both**: set it for scanned pages and the model starves on digital ones; set it for digital pages
and a scanned document floods it. Concurrency is therefore expressed in **requests**, not pages — one
semaphore caps what is outstanding at the model, and everything else exists only to cap *memory*.

Two rules follow, and both were learned the hard way.

**Nothing between that semaphore and the model may bound lower than it does.** The HTTP client pool is the
trap: a request that cannot get a socket blocks *inside* the client, holding its permit and sending nothing.
Requests are handed to clients evenly by count but not by duration — a layout call occupies its socket an
order of magnitude longer than a crop — so pools saturate unevenly, and a socket count sized "exactly right"
silently becomes the real bound. Sockets are therefore **derived** from the semaphore with several times the
per-client mean as headroom, never chosen as a literal.

**A memory bound must never become the throughput constraint.** Each is sized so the request semaphore runs
out first. Get either rule wrong and the symptom is identical to a slow GPU: the model idles, every knob
looks innocent, and nothing in our own logs says otherwise.

Which is why the stage line separates **waiting** from **working** — every span is one or the other, never
both. A span that mixes them cannot answer the only question worth asking. (This is not hypothetical: while
the spans conflated queue wait with model time, layout appeared to cost about the same as reading. Separated,
it is five to eight times more.)

Memory then follows from what each thing is needed *for*, not how long it is referenced:

- A **decoded bitmap** is needed to lay out and to cut — never to wait on the model, which is the long
  part. It is built, used, and dropped inside a bounded gate; a page waits out its requests holding only
  its (far smaller) encoded bytes.
- A **crop** is compressed the moment it is cut, so what is held during the wait is the wire format, not
  raw pixels.
- **Pages in flight** are bounded only because a page must be claimed before it can be laid out, and until
  it is laid out nothing downstream can account for it. It is a memory bound and nothing more: raising it
  does not feed the model. A page waiting on crop budget is a page whose crops are already cut and already
  *demanding* slots, so the semaphore saturates long before the page cap binds — measured, doubling pages in
  flight tripled the queue, produced no additional model work, and cost a gigabyte.

Host memory is therefore **flat** — a function of these bounds alone, not of how many documents are
uploaded, how large they are, how dense their pages are, or how long the queue gets. Backpressure
propagates the whole way back: requests fill → pages stop being admitted → rendered pages back up → render
stalls → paginate stalls.

### Reading pages (ocr)

A page is read differently depending on whether its characters already exist, because re-recognizing text
that is already present only introduces errors:

- **Digital page** (real text layer) — the vision model produces the layout and reads only the non-text
  regions; text and headings are taken from the page's own text layer at each block's box, so they are
  the document's own characters, not re-recognized. Blank and fill-in fields are then re-read from a close
  crop to catch ink the text layer lacks.
- **Scanned page** — the vision model reads the text; a lightweight secondary pass then adds only the
  lines it dropped, kept if confident and not already covered.
- **Non-text regions** marked as image but left empty — seals, stamps, logos, figures — are cropped and
  read as text; QR and barcodes are decoded to their real value, not described. Equations are kept as
  LaTeX.

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

Tables are separated **by schema**: a run of rows with consistent columns is one table; a schema change
starts a new one, so a single source table yields **one or more** canonical tables — a stacked invoice
splits into its summary block and its line-items.

By source:

- **Spreadsheets** — every cell, merge, table object and frozen pane is captured; contiguous regions are
  found and their structure resolved from anchors; column names are read from the header rows, not written
  by the model.
- **CSV/TSV** — types inferred deterministically; blank headers named from the column's position. No model
  for structure.
- **JSON** — shredded deterministically into normalized, joinable tables: nested objects flattened by
  dotted key, lists of objects into child tables keyed back to their parent.
- **Embedded HTML/PDF tables** — the grid is built deterministically (spans expanded), then the same
  structure step the spreadsheets use resolves multi-row headers and schema splits into one or more
  tables; types inferred from the cells and column names read from the header rows. Tables continuing across consecutive pages are
  stitched into one first — fragments with a matching column count across a page boundary merged, repeated
  headers dropped, page furniture skipped — so a forty-page table is structured as one.

Every table gets a description written for retrieval — its subject, what a row represents, and the
entities and vocabulary a user would search for. It is written at **finalize** (below), not during
ingestion, uniformly over native, HTML and scanned-page tables — a search artifact, never ingested data.
This description is what makes a table *findable*: a grid
of numbers and terse headers has almost no natural-language surface to match a question against, so without
a written description a search for the table's subject would miss it.

## Search representation

Citadel retrieves **leaves and tables, never chunks**. A leaf's search text is its own cleaned text plus
its context: library, filename, the headings above it, and the page's paratext; cleaning normalizes
Unicode, rejoins hyphen-split words, collapses whitespace, and turns machine names into words. A table's
search text is richer: library, file, sheet, title, caption, notes, its column headers, and its
description. Rows stay off the text path.

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
canonical table representations. Progress is a state per document (`queued → processing → ingested`, with
`failed` / `skipped` for the special cases) and per library (`ingesting → ingested → ready`).

Making a library *answerable* is a **separate phase from ingesting it**, run once per library after its
last document is stored — deliberately never interleaved with reading, so it can't contend with the vision
model for the GPU. Finalize does two things:

- **Describe every table.** Each canonical table — native, HTML, or lifted off a scanned page — gets its
  retrieval description written here, uniformly, as the search artifact above.
- **Embed and index.** Each leaf's and table's search text is embedded into the dense index and the lexical
  index is built, so the library becomes queryable.

Both are gated by the library's **tier**. A *structure* library is ingested to the lossless tree and
tables and stops there; a *search* library additionally gets the descriptions, embeddings and indexes that
Part II runs on. The tier is a pricing/access boundary, not a different pipeline — a structure library can
be upgraded and finalized later without re-ingesting. Finalize is event-driven: it fires the moment a
library's last in-flight document drains, and is re-checked on restart for any library that became ready
while the embedder was down.

---

## Part II — Query

Built. Runs over the persisted tree and tables: the model decides what to ask, the database computes the
numbers. The hard part here is not finding candidates — it is deciding, reliably and at scale, which of a
large body of evidence actually bears on the question, and fitting exactly that into one answer without
ever letting the model invent a figure or miscount what fits.

## Query pipeline

```bash
question → reformulate → retrieve ─────────→ filter ──→ compute ──→ unify ──→ fit ──→ synthesize → cited answer
             (model)      text ‖ tables        (model)   (model +    (model)  (exact)          (model)
                          dense + lexical                 database)   essential /
                          → fused wide net                            supporting
```

| Stage | Work |
|---|---|
| reformulate | rewrite the question for recall; translate into the corpus language |
| retrieve | search text and tables as separate channels, each dense + lexical, fuse each channel into one **wide net** |
| filter | keep only the evidence that bears on the question — the model judges, over the net, in batches, stopping when the ranking goes dry |
| compute | the model writes one read-only query per kept table; the database executes it and returns exact values |
| unify | across both kinds of evidence, mark each item essential or supporting; drop the rest |
| fit | pack the kept evidence into the answer's budget: essential before supporting, large tables shrunk not dropped |
| synthesize | compose one cited answer from the passages and the computed results |

### Retrieve — a wide net, not a short list

Two channels — text and tables — each searched by meaning (dense) and by exact match (lexical) for every
reformulation, then fused by reciprocal-rank fusion (rank-based, so text and tables need no comparable
scores). Rows are never searched; a table is found through its description and columns and answered by SQL.

Each channel returns a **wide net, not a fixed top-K**. The reason is that a fused rank is an *ordering*,
not a relevance judgment: it can say one candidate matched more strongly than another, but not whether the
top one actually answers the question. A short top-K would stake the answer on that ordering. Instead the
net is deliberately broad and the relevance decision is handed to a model that reads the candidates. The
ordering is not thrown away — it is used only to decide how deep to look.

### Filter — the model judges relevance; the rank only bounds the search

The model reads the candidates and keeps the ones that bear on the question: for a passage, its text; for
a table, its columns and sample rows — **not** its description, which the model itself wrote, so it would
be grading its own summary. It is allowed to keep nothing.

This stage is where three otherwise-tempting shortcuts are deliberately refused:

- **No score threshold.** A cutoff on the retrieval score would throw away relevant evidence that happened
  to score low — and low-scoring evidence is often still relevant. Relevance is the model's call, not a
  number's.
- **No reranker.** A cross-encoder can score how well a passage matches a question, but it cannot score a
  value a query has not produced yet — and once the model reads the whole net, a reranker adds nothing it
  isn't already doing.
- **No arbitrary cap on how much to read.** The net is judged in batches, best-first, and the search stops
  after the ranking goes dry for a few batches in a row. Relevant evidence clusters near the top of the
  ranking, so once it stops appearing, the tail is almost always empty. This is how a net of hundreds or
  thousands is filtered while the model reads only the part that carries signal — the ranking is trusted
  to *bound the search*, never to *make the selection*.

### Compute — the model writes the query, the database runs it

For each kept table the model writes **one read-only query** against that table's columns, referring to
columns by position from a map of index to header and type. The query runs through a typed
projection that turns the stored cells into typed, named columns, inside a **read-only sandbox**: SELECT
only, no other tables, a time limit. The model writes the logic; the database computes every value — the
same integrity boundary as ingestion, so no figure is ever aggregated or invented by the model.

Queries are run *before* the final selection, on purpose: the next stage then judges tables by their
actual computed results and true sizes, not by a guess at what they might contain.

### Unify — essential vs supporting

The passages that survived and the computed results go back to the model together. It makes the final
cross-modal selection — dropping anything that, read against the computed answer, turns out not to be
needed — and marks each remaining item **essential** (the answer is wrong or incomplete without it) or
**supporting** (it corroborates or adds context, but the answer stands without it). This priority is the
input to fitting.

### Fit — keep what matters, never miscount

The kept evidence is packed into the answer's budget, and this is done **deterministically, by the system,
not the model** — because a model cannot reliably count tokens, so the hard size limit can never be its
job. The rules:

- **Essential before supporting.** Priority, not size, decides what survives contention; a large
  supporting item can never displace an essential one.
- **A large table is shrunk, not dropped.** A table result too big to fit keeps a fair share of its rows
  — distributed round-robin across the kept tables so no one table starves the others — and is marked as
  showing part of a larger result. Because a table is divisible, "essential but large" resolves to
  "essential, represented by its top rows," never to "gone."
- **Passages are atomic, kept by rank.** They can't be trimmed without mangling prose, so they're included
  whole in priority order until the budget is spent.

The division of labor is the point: the model decides *what matters*, the system decides *what fits*.

### Synthesize

One model call merges the passages, each carrying its document and page, and the computed results, each
carrying its table, into a single cited answer. Computed results are the authority for figures and totals;
passages supply narrative and context; there is no single numeric ranking across the two — the model is
where they reconcile. If the evidence does not answer the question, it says so plainly. For a search
interface that wants a ranked list of hits instead of prose, the channel rankings are fused the same
rank-based way.

## Models

One general model, run without a separate reasoning pass, performs every model step across ingestion and
query — table structuring and description, reformulation, filtering, query writing,
priority, and synthesis. A vision model reads documents that exist only as pixels. A multilingual dense
representation carries meaning for search. Nothing in the pipeline asks a model to hold data in its head:
it reads, judges, and writes queries; the database keeps the numbers.

---

## Known limitations

Open gaps in the current build. None corrupts an answer — each is a place the system is weaker than the
design intends.

- **Key-value forms mis-structured as tables.** The table-structure step assumes a grid: a header band
  over data rows. A form or invoice laid out as label/value pairs (an applicant form, a cash-sale invoice)
  has no such band, so header-row detection latches onto the wrong rows — a date, a reference number, or a
  name becomes a "column header" while the real fields sit in the cells. The table still materializes and
  never blocks ingestion; it is simply a poor representation of a document that was never really a table.
  The filter ignores these when they don't bear on a question, so the effect is confined to queries
  actually about such a form.

- **Stray query on a prose question.** A purely narrative question ("what is this dispute about")
  sometimes still draws a single read-only query against a loosely related table. It is harmless — the
  query is single-table and its result is dropped at unify when it turns out not to answer the question —
  but it spends a model call and a database round-trip it did not need.

- **Filter breadth on broad questions.** The relevance filter is tuned to be precise, which serves pointed
  questions well but can keep too little for an open "summarize everything about X": the answer is correct
  but thinner than the corpus could support. This is a prompt-tuning axis, not a structural limit.

- **Throughput is bounded by the visual model, and layout is most of it.** Every CPU stage — converting,
  rendering, cutting, text-layer extraction, gap-fill — is a rounding error beside the GPU; measured
  end-to-end, a page's time is almost entirely the two model calls, and **84–89%** of that is the single
  **layout** request, which returns only boxes but must write out every block it finds. So more throughput
  comes from a faster or smaller vision model, or from asking it for less — not from more concurrency, and
  not from more host RAM. The largest available win is a model whose layout stage is a **detector** rather
  than a generation: it deletes the dominant cost outright, and cannot fail the way a generation can (a
  degenerate layout generation occasionally returns zero blocks for a whole page).

- **The GPU idles 30–60% of the time on a full queue, and we cannot explain it.** The model reports every
  sequence slot occupied and nothing queued behind them, while the card shows no kernel resident for much of
  the run — at high clocks and low power, which is an idle GPU, not a saturated or throttled one. It is not
  page supply (doubling it did no extra work), not the crop semaphore (crops queue 6–18× deeper than they
  run), not the socket pool, not the inference server's frontend (more of them changed nothing), and not
  power or heat. This is the single largest known gap between the pipeline's throughput and the hardware's.

- **Documents are admitted strictly FIFO, so a large one blocks the queue behind it.** Render jobs enter one
  ordered stream in the order documents are paginated, and a stream cannot be skipped — so an 800-page book
  monopolizes every page slot until it drains. Measured: a one-page PDF waited **587 seconds** to be looked
  at. It costs little wall time (total work is unchanged by order) but it starves the tail, where the only
  work left comes from documents too small to fill the model, and it makes small files finish last.
  Fair-share admission across documents is the fix; it has not been built.

- **A memory bound that becomes the throughput constraint is invisible from the outside.** The failure
  looks identical to a slow GPU: the model idles, the queue drains, and every knob looks innocent. It is
  only distinguishable by asking where a page's time actually went — which is why every span in the stage
  line is either a wait or a work, never both. Tuning these by inference rather than measurement reliably
  picks the wrong one.

- **Run-to-run variance is ±5%.** Identical configurations have measured 810s and 843s. Any single-run
  comparison inside that band is noise, and treating it as a result is how most of a day gets spent tuning
  constants that never mattered.

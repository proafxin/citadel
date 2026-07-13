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
upload → normalize → paginate → render → ocr ──────────────────→ merge → relational store
                     └──────────────────→ tabular ─────────────→ merge
```

| Phase | Work |
|---|---|
| normalize | route + convert to one of the three lanes |
| paginate | count PDF pages and emit one render job each; or split markup/text/tabular into units |
| render | rasterize one PDF page |
| ocr | detect the page's regions, then read each one → blocks |
| merge | blocks → split paratext → stitch tables → structure embedded tables → content tree → persist |
| tabular | per sheet/file: structure → canonical Table (its retrieval description is written later, at finalize) |

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

The detector runs on every page and produces the same thing regardless: regions, their classes, and the order
a human would read them in. What happens to each region then depends on whether its characters already exist,
because re-recognizing text that is already present only introduces errors:

- **Digital page** (real text layer) — prose regions are taken from the page's own text layer at each
  region's box, so they are the document's own characters, never re-recognized, and never sent to the model.
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
query — table structuring and description, reformulation, filtering, query writing, priority, and synthesis.

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

- **Throughput is bounded by the GPU's power limit, and there is no lever left but doing less work.** The
  card runs pinned at its cap — 142 W of 145 W, clocks sagging from 2205 to 1867 MHz as it heats — at 93%
  utilization, with the inference server reporting a full set of running sequences and *nothing waiting*
  behind them on essentially every log line. It is saturated. Adding concurrency, workers, page supply or
  host RAM anywhere in the pipeline does nothing, and this is now provable rather than inferred. The only
  remaining gains come from sending the model fewer or smaller crops (below), not from feeding it faster.

- **Every crop is upscaled to a minimum pixel count, and small ones pay for it.** A crop is smart-resized
  into a fixed pixel window before it is sent. The floor exists so a one-line crop arrives legible — but it
  means a 27×22 page number is upscaled roughly 190× and still bills a full ~183 image tokens. Since the
  workload is prefill-bound, that floor is the single largest remaining cost, and lowering it is a direct cut
  across *every* crop. It has not been changed, because it is the reference pipeline's value and it trades
  directly against legibility: it needs a quality comparison, not an argument.

- **Page furniture is re-recognized on born-digital pages.** Headers, footers and page numbers are cropped
  and sent to the recognition model even when the page has a text layer that already holds them exactly.
  They are paratext — never leaves — so this is both wasted prefill and a needless re-recognition of
  characters we already had. Taking them from the text layer, as every other prose region on a digital page
  already is, is the fix. It is worth roughly 6% of crops on the current corpus: the arithmetic is dominated
  by one 809-page *scanned* book that alone accounts for 80% of every crop in a run.

- **Recognition requests are dropped intermittently, and the cause is not established.** About one crop in
  sixteen thousand fails with the server closing the connection without a response; the server logs nothing
  and continues serving a full batch through the failure. It reproduced once in a run and not at all in the
  next, at identical settings. No fix has been shipped for it, because no cause has been proved — several
  plausible ones (keep-alive expiry, socket-pool exhaustion, event-loop starvation) were each tested and
  each disproved. The server's access log is now enabled so the next occurrence distinguishes "the request
  reached the application and was dropped" from "it died beneath it," which are different bugs.

- **Documents are admitted strictly FIFO, so a large one blocks the queue behind it.** Render jobs enter one
  ordered stream in the order documents are paginated, and a stream cannot be skipped — so an 800-page book
  monopolizes every page slot until it drains. Measured on a 426-second run: a 13-page PDF waited **403
  seconds** to be looked at, then took 6 seconds to read. It costs little wall time (total work is unchanged
  by order) but it starves the tail, where the only work left comes from documents too small to fill the
  model, and it makes small files finish last. Fair-share admission across documents is the fix; it has not
  been built.

- **A memory bound that becomes the throughput constraint is invisible from the outside.** The failure
  looks identical to a slow GPU: the model idles, the queue drains, and every knob looks innocent. It is
  only distinguishable by asking where a page's time actually went — which is why every span in the stage
  line is either a wait or a work, never both. Tuning these by inference rather than measurement reliably
  picks the wrong one.

- **Run-to-run variance is a few percent.** Identical configurations have measured 426.4s and 426.8s on the
  current build, and drifted several percent on the previous one. Any single-run comparison inside that band
  is noise, and treating it as a result is how most of a day gets spent tuning constants that never mattered.

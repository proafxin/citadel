# Citadel Architecture

How Citadel turns an uploaded file of *any* format into a **queryable relational knowledge layer** — a
lossless content tree plus canonical tables in a relational database — and then answers a question over it
by having the model **write a query the database executes**, so figures are computed, never guessed. Both
halves — ingestion and query — are built.

The single idea the whole system is organized around: a document should become **data, not searchable
text**. Once it is data, the model is only ever asked to do things a language model is good at — read
layout, judge relevance, write a query — and never the things it is bad at: parsing a messy grid,
aggregating thousands of rows, or inventing a number. Every design decision below follows from holding
that line.

One general-purpose vision-language model performs every one of those steps, across both halves — reading
pages and embedded images, structuring tables, summarizing, resolving a question, writing queries, and
composing the answer. There is no separate layout/detection model and no separate text-only model: the same
model that reads a page's pixels also structures its tables and writes the query over them.

---

## Part I — Ingestion

## Goal

Ingest **every** format into a **lossless**, persisted, *queryable* representation — no chunking, no
flattening tables into prose, and no model ever inventing or computing a data value. The end state is
data, not searchable text: a content tree plus canonical tables in a relational database, ready for
structured queries.

## Three lanes

Every format reduces to one of three extractors, chosen at intake:

| Lane | Sources | Output |
|---|---|---|
| **Markup** | HTML; Markdown, Word, ODT, RTF, EPUB (converted to HTML) | content tree |
| **Visual** | PDF, images | content tree |
| **Tabular** | spreadsheets; CSV/TSV; JSON | canonical table + rows |

Slide decks (PowerPoint, ODP) are a fourth path, distinct from all three: they are read directly from their
own shape/text structure (never rendered to page images), with embedded images read individually — see
"Reading pages" below.

Plain text is a degenerate markup case: split on blank lines into paragraphs.

### What each format becomes

| Source | Lane | How |
|---|---|---|
| spreadsheets (XLSX / XLS / ODS) | tabular | cells read directly |
| CSV / TSV / JSON | tabular | typed parse / structural shred |
| XML | tabular | shredded to the same normalized shape as JSON, then the JSON path |
| HTML | markup | parsed as-is |
| Markdown | markup | converted to HTML (math kept as LaTeX) |
| Word / ODT / RTF / EPUB | markup | converted to HTML |
| PowerPoint / ODP | — | read directly from its shapes/text, never rendered to page images |
| PDF | visual | every page rendered to an image and read whole by the vision model |
| images | visual | read as one page |
| SVG | visual | rasterized to a raster image, then read as one image — SVG carries no accessible data path today, so a chart authored only as SVG is read the same as any other picture |

## Pipeline

Ingestion runs as a set of always-on streaming stages connected by a shared queue, not a per-upload script —
an upload joins a continuously running pipeline rather than triggering its own isolated job. Each stage is an
independent, asynchronous consumer running concurrently with every other stage, and work is **page-granular**:
the unit of work is a page, not a document, so a whole corpus is read in parallel rather than one document
finishing before the next begins. Each stage claims only as much work as it currently has room for, and a
slot frees the instant one item completes — never in batches — so nothing waits behind an arbitrary group
boundary.

The pipeline has three parallel intake paths, one per lane, that converge on shared closing phases:

- The **visual** lane renders each page to an image and reads the whole page with the vision model in one
  pass, tables included (see "Reading pages" below).
- The **markup** lane parses HTML/PowerPoint/plain-text structure directly — no rendering, no vision model; a
  format-specific parser produces the same block shape synchronously.
- The **tabular** lane resolves each spreadsheet sheet, or each flat table file, into its canonical shape
  directly — see "Tables" below.

The visual and markup lanes converge on a shared **structuring** phase once every page or unit of a document
has been read: it splits out running headers/footers, reclassifies and stitches candidate tables, resolves
each bounded table's internal shape with one model call (see "Tables" below), and hands the finished blocks to
**merge**, which assembles the content tree and persists it together with any finished tables. The tabular
lane resolves its own table structure inline and converges on the same merge phase. A document with no table
needing a model call merges as soon as its pages or units are read — it is structured and stored *during* the
run, not held behind work it doesn't have. (Becoming *searchable* is a later, separate step — see "Output and
finalize" — not part of this pipeline.)

The queue carries only lightweight in-flight state — page images and per-page blocks. The uploaded source
itself is held once, in a shared store, for the length of the run, and read directly by whichever stage needs
it, rather than being copied through the queue per page.

### What bounds the pipeline

There is no layout detector and no crop step. A page is rendered once, whole, and read once, whole: the
rendered image is sent to the vision model as a single request asking for the page's content directly. This
replaced an earlier two-model detect-then-crop-then-read design — a classifier that tried to skip pages whose
text came from the source document's own text layer was found unable to reliably rule out tables, lists, or
other non-plain-text structure on a "digital" page, so that fast path was dropped entirely: every page,
digital or scanned, goes through the vision model unconditionally. Splitting a page into per-region crops read
separately was retired along with it.

**Every call class that reaches a model-serving backend has its own admission pool, sized to its own cost
profile, and every pool is shared across however many copies of the application are actually running, not
local to one.** A page-reading request carries the full image cost of a rendered page; a table's
internal-structure call carries only the text of a bounded grid; a summarization or a large query-resolution
call carries a much bigger prompt and runs far longer; an embedding call is small and high-volume. Running all
of these under one shared limit would mean the cheap, fast calls queue behind the expensive, slow ones for no
reason, and a limit sized for one profile would starve the others. Admission to a pool is one atomic check
against a shared, durable counter — correct regardless of how many processes check it concurrently, since the
counter is the single source of truth, not any one process's memory. A caller that can't get a slot waits for
a release signal rather than polling in a loop, with a bounded fallback wait so a missed signal costs at most
one extra interval, never a stuck wait. A slot whose holder crashes mid-call is not leaked forever — an
expiry on every held slot reclaims it automatically on the next attempt by anyone.

Four pools exist, each sized to its own call class's real cost, not to one shared guess:

- **Vision** — page rendering and page reading, plus embedded-image reading, share one budget. A rendered
  page's slot is held from the moment it is claimed for rendering until that same page's reading call
  resolves — one slot spans both steps, because they are really one occupancy of the vision pipeline for that
  page, not two independent draws. This is what keeps host memory bounded regardless of how many documents or
  pages are in flight: a page cannot be rendered far ahead of what the model can actually read.
- **Table/document structuring** — every vision-read and markup-sourced table's own structuring call draws
  from this pool, sized considerably higher than the vision pool because a text-only structuring call is
  materially cheaper and faster per request, not because more of them run in total.
- **Large-prompt reduction** — the smallest of the four, because this is the opposite profile from
  table/document structuring: summarization (both the per-section pass and the per-document reduction), query
  resolution and filtering, evidence merging, and a spreadsheet's own multi-candidate table-structuring call
  (which shares this slow/bursty profile, not single-table structuring's fast/steady one) all draw from this
  pool. Sharing one pool with the high-volume single-table-structuring calls would let a burst of many
  summaries at the end of an ingestion run occupy most of that pool's slots and starve a
  concurrently-ingesting library's table structuring — a small, dedicated, low-concurrency pool is what keeps
  a slow/bursty call class from crowding out a fast/steady one, in either direction.
- **Embedding** — sized to match the embedding service's own concurrency ceiling, since embedding calls are
  small enough per request that the service itself, not the client-side pool, is the real limit worth sizing
  against.

A separate, smaller, process-local limit bounds how many documents any one process is concurrently walking
into the rendering pipeline at all — this one is intentionally not shared, since it bounds CPU-side rendering
work local to each process rather than a shared model-serving backend; a document queued behind it is exactly
the intended backpressure, keeping rendered-but-unconsumed pages from piling up in memory ahead of what the
model can actually process.

Beneath all of this sits a bound the system does not control: each inference server's own scheduler admits
requests by available compute/memory, not by request count, so the number of requests actually executing
concurrently can be well below what's been admitted client-side — the rest queue inside the server itself.
That queue is not starvation (the server is fully utilized while it's non-empty), but it does mean a naive
elapsed-time measurement on an individual request conflates real processing time with however long that
request waited behind others for a slot. The page-reading call gets an honest split without any server-side
instrumentation, by streaming its response and timing to the first generated token: everything before that is
queue/scheduling wait, everything after is generation — reported separately per page. This isn't a perfectly
isolated measurement (generation time still includes the server's own interleaving with other concurrently
running requests), but it separates queue wait from compute in a way a single blocking call cannot.

The vision and structuring pools deliberately sit above the inference server's own real concurrency ceiling —
the server admits by available capacity, not by request count, so excess concurrency queues safely inside it
rather than running in parallel; that is the intended shape, not a flaw, and is what keeps the client-side
pools from ever being the thing that starves the model.

### Reading pages

Every page — digital or scanned, dense or sparse — is rendered to one image and read by the vision model in
one request. There is no region split and no per-block routing: the model returns the page's structured
content directly, as markdown, in one pass — headings, tables, lists, code — and that response is parsed
into blocks.

**Genuinely blank pages are detected before the model ever sees them.** A page's rendered image is checked
for near-zero pixel variance first; a page that is blank produces zero blocks without a model call, rather
than being sent for reading. This exists because a vision-language model asked to read a page with nothing on
it does not reliably say so — it can instead produce fluent, structurally plausible content that is not on the
page at all, and a blank-page guard is the only thing that closes that off entirely rather than depending on
prompt wording alone.

**The model's own meta-commentary is stripped before parsing, not stored as content.** Even when correctly
declining a page (a blank one that slipped past the guard, or one it judges unreadable), the model's response
can carry a preamble or refusal sentence around the actual content — "here is the transcription...", "there
is no visible text on this page" — and a whole-response wrapper some replies are wound in. Both are
recognized and removed from the raw response before it is split into blocks, so a refusal never becomes a
stored content row and a wrapper never causes the whole page to be read as one opaque block by the
code-block detection described next.

**The table/list/code distinction is asked for explicitly, in the same pass, rather than left to the model's
default.** The prompt tells the model: tabular or record-like data — including a label/value form, a
transposed layout, or a crosstab — becomes a table shaped by its *true* row/column structure, not a literal
mirror of the visual layout; a list or enumeration that is not tabular data stays a list; code, a formula, or
an algorithm listing is kept as its own block with line breaks preserved. This matters because a prompt that
only describes what a table should look like, with no equivalent instruction for lists or code, pushes the
model toward tables as its only structured option — a plain numbered list of clauses can come back as a table
with mostly empty cells instead of a list.

Code blocks are recognized as their own block type by splitting the response on its own boundary markers
before the rest is parsed, so a page containing a code sample keeps its exact indentation and line breaks
rather than being flattened into a paragraph alongside everything else.

**Embedded images** — inside Word, spreadsheet, PowerPoint, HTML and EPUB sources, not just PDF pages — go
through the same vision model individually, so a table or text baked into an image is not lost. Before an
embedded image is sent, it is size-filtered (decorative icons/logos below a pixel threshold are dropped
rather than read) and content-hash deduplicated: identical images that recur across a document (a repeated
letterhead or logo) are read once, and the result is reused for every occurrence rather than re-reading it
each time. Both checks share the same admission budget as page rendering above, so embedded-image reading
cannot bypass the memory bound that page rendering is subject to. A standalone SVG file is rasterized the
same way a PDF page is and then read as one image — SVG has no accessible-data path today, so this is the
same visual-lane treatment any picture gets, not a special case.

Markup and visual pages converge on the same block shape, so everything after is format-agnostic.

## The content tree

Each document becomes one tree: **document → sections (nested by heading depth) → leaves**. Sections carry
a depth and a label; leaves carry content. Pagination is **provenance on the leaf** (page/sheet + position),
never a tree level. Every node has a deterministic id, derived from where it lives — library, document, page,
and position — so the same content always resolves to the same id; a table uses the same scheme.

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
| Running headers/footers/page numbers | not a leaf — folded into that page's leaves' search text; the printed page number kept as text, page position kept as provenance |
| Embedded tables | first-class canonical tables (below) |

## Tables: the integrity boundary

Every table — spreadsheet cells, CSV, JSON, an HTML table, or a table on a scanned page — becomes one
canonical shape: typed columns (each a header and a type), the full rows, a sample, a description, its
title/caption/notes, and provenance. The full rows are **stored as relational rows and queried by structured
query — never embedded.**

The rule that makes it trustworthy: **the model emits structure and descriptions, never data.** How much it
sees varies — an anchoring view of a large markup table, a spreadsheet sheet in full — but what it returns
never does: a shape, in coordinates. Every cell is copied verbatim from the source by the code that reads it,
and column types are inferred from the values. This is the same
boundary the query side relies on: the model writes the query, the database computes the values. It exists
because the failure mode of every "AI reads your spreadsheet" system is the model quietly misreading or
re-adding a number — so the model is never in a position to touch a value.

A second rule decides *when* a model is asked at all, and it splits into two different questions that used
to be conflated: **where do a table's boundaries lie**, and **what is its internal shape**. Boundaries are
genuinely ambiguous only for a **spreadsheet's raw cell grid** — tables can begin anywhere on a sheet, several
can share one, and nothing in the file says which cells belong to which table. That is the one place a model
is shown the sheet **whole** and asked to account for every row of it.

Internal shape — which rows are the header, whether the table is transposed, where a section label sits — is
a narrower question that a *bounded, single* table can also need answered even when its boundaries were never
in doubt. An HTML table or a PowerPoint table/chart object is already exactly one table; nothing needs to
decide where it starts or ends. But its header is not always explicit in the source — the same label/value
form, transposed layout, or crosstab shape a scanned page can carry shows up in real markup just as often, and
a mechanical "the first row is the header" guess gets those wrong the same way it would for a scanned page. So
that one bounded table is still shown to the model — one grid in, one structure out, no candidate list, no
merge/drop logic, because there is nothing to merge or drop. This is a materially cheaper call than the
spreadsheet path: no token-budget packing across multiple candidates, no multi-table splitting judgment,
because a single already-bounded table never needs either.

A page read by the vision model converges on the same mechanism as a markup-sourced table, not a separate
one: the vision pass produces the page's structured text, a table-tagged region of it becomes a grid the same
way an HTML table does, and that grid goes through the identical single-table structuring call. What differs
is only how much of the grid the call is shown. A vision-read table is bounded by construction — a single
page's whole reading output is itself capped, so any table on it is provably small enough to show in full,
uncapped, with no sampling — while a markup-sourced table has no such bound (a source table can genuinely be
arbitrarily large), so it is shown as much of itself as fits a dedicated budget for that call, falling back to
an evenly-spread sample of the whole table only when it doesn't fit. Earlier, only markup-sourced tables got a
structuring call at all, and a vision-read table's shape was guessed mechanically from the grid alone — that
mechanical guess is the source of a still-open failure mode on tables with deep, two-level nested headers;
routing every bounded table through the same structuring call, vision-sourced or not, is the fix in progress
for it.

The routing is therefore by **ambiguity, not by source** — the distinction matters, because a source-shaped
rule invites a private path per format, and the whole point is that there is exactly one. Every table from
every origin converges on a grid, one step turns a grid plus a structure into the canonical shape, and the
only thing that varies is whether that structure was read from the format or asked of a model.

Tables are separated **by schema**: a run of rows with consistent columns is one table; a schema change
starts a new one, so a single source table yields **one or more** canonical tables — a stacked invoice
splits into its summary block and its line items.

A table also has a **layout**, which the structuring step reports alongside the columns: *relational* (each
column is a field, each row a record) or *crosstab* (a matrix — one measure spread across many columns
labelled by the header rows, for example a grid of years across the top with a value in each cell). A crosstab
is **denormalized to long form** at ingestion: the key columns that identify a row, one column per header-row
dimension, and a single value column, so one wide row becomes many narrow ones — a wide indicator sheet with
dozens of year-columns becomes one relational table of country/indicator/year/value rows, each row a single
data point, which is what makes it answerable by an ordinary filter. The distinction matters because a
crosstab left wide is unqueryable: "the value for one country in one year" is a *column name* in a wide table,
not something a filter can reach.

The spreadsheet path is designed to be lossless cell-for-cell, including through denormalization: a crosstab's
long-form row count always equals its source's non-empty cell count, by construction, not by post-hoc
adjustment — the reduction is arithmetic on the source region, so nothing is invented or dropped in the
reshape.

By source:

- **Spreadsheets** — every cell, merge, defined name, table object, hidden row and frozen pane is captured
  into a text dump of the whole sheet, and that dump — not a sample of it — is what the model is shown.
  **The one ambiguous source, and the only one that costs a structure call for boundaries as well as shape.**
  Column names are still derived from the header rows the model points at, never written by it.
- **CSV/TSV** — one schema by construction. The parser handles quoting, embedded newlines and ragged rows,
  skips leading blank lines, and names the columns from the first non-empty row. No model.
- **JSON** — shredded deterministically into normalized, joinable tables: nested objects flattened by key
  path, lists of objects into child tables keyed back to their parent. An entity's keys are its schema, so
  there is nothing to infer. No model.
- **Tables on a rendered page (PDF, images)** — the grid is built deterministically from the vision model's
  own reading output for that page (table markup parsed into cells), and its structure — which row is the
  header, orientation, section labels — is asked of the model exactly like a markup table, shown the whole
  grid uncapped rather than a sample, because a single page's reading output is itself bounded so the table on
  it is provably small enough to show in full. Tables continuing across consecutive pages are stitched into
  one first — fragments with a matching column count across a page boundary merged, repeated headers dropped,
  page furniture skipped — so a forty-page table is one table.
- **Embedded HTML/PowerPoint tables and charts** — the grid is built deterministically with spans expanded
  (a native chart's own category/series/value data is read directly, the same way a spreadsheet cell is — no
  vision, no model, genuinely lossless). Its structure — header row(s), orientation, section labels — is
  asked of the model exactly as described above: one bounded table, one call, no candidate list. The header is
  not assumed from markup alone, because an explicit header marker in the source states *that* a row is a
  header far more reliably than it states that a form-shaped or transposed table needs restructuring rather
  than a literal read. Unlike a vision-read table, a markup-sourced table's source size has no natural cap — a
  source table can genuinely be arbitrarily large — so it cannot always be shown in full: it is shown as much
  of itself as fits a dedicated budget for that call (top rows, bottom rows, and an even spread of the middle
  when it doesn't all fit), never a flat row count.

**The spreadsheet call is one sheet in, one structure out, and the sheet is shown whole.** Pre-segmenting the
grid into candidate regions was tried and rejected. Geometry cannot decide a spreadsheet's boundaries: a blank
row separates two tables on one sheet and is decorative spacing between every data row on another; a lone
string in column A is a totals row in one place and the next table's caption two rows later, with nothing
geometric distinguishing them. A connected-component pass over a real corpus split a single table into ten
fragments and merged two unrelated ones, and the errors ran in both directions at once. The asymmetry settles
it: a model shown the whole sheet can always split it, but a model shown fragments cannot merge what it was
never shown together — over-segmentation is unrecoverable, under-segmentation is just a judgment the model
makes with the evidence in front of it.

So the sheet is rendered to text losslessly — every row including blank ones, every populated cell with its
formula, then a fixed metadata block carrying merges, defined names, number formats, hidden rows and bold
cells — and the model returns every table in it plus a role for **every row**. Blank rows are printed rather
than skipped because they are the primary boundary signal; formulas travel inline because an aggregate names
its own body extent (`=SUM(J12:J15)` says the rows above it are one table's body); defined names travel
because authors often declared the table already. None of that is explained to the model — the dump carries
the evidence and reading it is ordinary comprehension.

Completeness is enforced structurally rather than asked for. The returned row roles must cover the sheet's
extent exactly once with no gaps and no overlaps, every table's rows must fall inside its own declared extent,
and no two tables may occupy intersecting rectangles — intersecting *rectangles*, not rows, because side-by-side
tables legitimately share rows in different columns. A failure returns the specific violated rows to the model
and it revises; the loop is bounded, and a span it genuinely cannot explain is reported as unresolved rather
than guessed at, so a gap surfaces as a gap.

Confidence is **computed, not reported**. Asked to self-assess, a model will cite a defined name and then
return an extent that contradicts it, in the same response — it has no point at which it compares its own
conclusion against the source it just quoted. So the extent is checked in code against the declared structure
already parsed out of the sheet: a defined name or merge whose rows coincide with the extent, or an aggregate
range that covers the body within the one row of padding authors habitually leave. Agreement makes it
corroborated; nothing independent makes it inferred. That is a claim about evidence, not correctness.

Structure detection still over-produces — a block of prose gridded into cells, a caption read as a one-row
table, a region that is not tabular at all. Membership is not a separate step: the same call reports the
non-table regions explicitly as blocks, so a title, a note, a legend, a key-value form or a packed text column
is named as what it is rather than silently dropped or promoted to a table. The model decides *membership*,
never content — the same boundary as structure.

A sheet too large to show whole is the one case this does not yet cover; windowing it without reintroducing
the fragmentation problem is open work, and the four sheets that hit it in the working corpus are the test
for it.

## Search representation

Citadel retrieves **leaves and tables, never chunks**. A leaf's search text is its own cleaned text plus its
context: library, filename, the headings above it, and the page's running text. Cleaning normalizes Unicode,
rejoins hyphen-split words, collapses whitespace, and turns machine names into words. A table's search text is
assembled the same way, from what the table already holds: library, file, sheet, title, caption, notes, and
its column headers. It is built mechanically, not written by a model. Rows stay off the text path.

Search text is indexed two ways: dense multilingual vectors for meaning, and exact lexical matching
(substring and fuzzy) for the names, identifiers and codes a meaning vector blurs together. A table is
*found* through its search text and *answered* by a structured query over its rows.

## Storage

- A **doc-keyed source store** holds each uploaded file for the length of its run — the interface a durable
  object store will later fill. Every stage that renders or reads the source reads it directly from here, so
  a large file is materialized once, not copied through the queue per page; it is deleted when the document
  completes.
- A **streaming queue** holds the pipeline's in-flight state (page images, per-page blocks) — ephemeral,
  bounded by backpressure so its memory stays flat regardless of upload size, and cleared after merge.
- A **relational database** holds the durable content tree, the tables and their rows, and the search
  indexes — this is what queries run against. The rows of every table are stored together and projected
  into typed columns on demand, rather than materialized as one physical table each: a real corpus has far
  too many tables for a physical table apiece.
- A **durable object store** holds the assembled per-document tree for download.

## Reliability

- Delivery is at-least-once and self-healing: a crashed run's pending work is reclaimed and reprocessed —
  including when the worker that crashed never comes back at all. Each worker's identity in the queue is
  derived from the machine or container it runs on, not a manually assigned id, so nothing has to be
  configured per replica and nothing breaks if an autoscaler replaces a worker with a differently-identified
  one. Work left behind by a worker that never returns is automatically reclaimed by whichever worker is still
  alive once it has sat unclaimed past an idle threshold — a killed worker's in-flight page is picked up
  elsewhere, not lost.
- Recording a page is idempotent, so a redelivered page is a no-op.
- A single merge fires exactly once — when the last page or sheet of a document completes.
- The trigger that starts post-ingestion processing (below) is claimed by exactly one consumer, not broadcast
  to every one of them, so running more than one copy of the application does not cause the same library to be
  finalized twice; the action itself additionally refuses to start twice concurrently for the same library, so
  even a redelivered or duplicate trigger is a no-op rather than a second concurrent run.
- Work that fails is retried a bounded number of times, then given up cleanly.
- Failures are scoped: a page that can't be read leaves a marker and the document still completes as
  *partial*; a whole-document failure is marked *failed*.

## Output and finalize

The result of ingestion is the materialized tree — sections and leaves with their content, and full
canonical table representations. That is the ingestion contract, and it draws a hard line: **ingestion
produces the queryable *structure*; it does not produce retrieval.** Once the tree and tables exist the
library is queryable by structured query, but not yet **retrieval-queryable**, which needs the summaries the
resolver reads. Progress is tracked as a state per document — queued, processing, ingested, with partial,
failed, or skipped for the special cases — and a state per library — ingesting, ingested, ready, or failed if
summarization or embedding could not complete after retrying.

Making a library *answerable* is a **separate prep phase**, run once per library after its last document is
stored — deliberately never interleaved with reading, so it can't contend with the vision model for the same
GPU. Everything in it is a *reduction over already-persisted structure*, not structure itself, which is
exactly why it belongs after ingestion rather than inside it:

- **Summarize.** Every document is packed into sections and each section summarized, then the document's own
  summary is reduced from those — the whole library in one pass. These are what the resolver reads, so a
  library is not retrieval-queryable until they exist.
- **Embed and index.** Each leaf's and table's search text is embedded into the dense index and the lexical
  index is built.

Summarization and embedding do not depend on each other, so they run **concurrently** and the library is ready
once both land — summarization runs on the same general model that reads pages, embedding on a separately
served embedding model, and running them together hides the shorter one (embedding) under the longer one
(summarization) instead of stacking the two. Summaries used to stream per document *during* ingestion,
overlapping page reading; that overlap paid a contention tax on the shared model and, worse, let a query in
the window resolve over a partial inventory. Running them in this later phase, gated on the library's last
document draining, closes both.

**Readiness itself is a durable, crash-recoverable process, not an in-process wait.** The signal that starts
the prep phase is claimed by exactly one consumer even when several copies of the application are running, not
broadcast to every one of them — a broadcast would mean every running copy independently starts the same
library's prep phase off the same signal; a claimed trigger instead lets them compete for the same signal, so
exactly one claims it, and the action itself refuses to start twice concurrently for the same library, so a
redelivered or duplicate trigger is a no-op rather than a second concurrent run. Once claimed, the prep phase
doesn't block on anything itself — it starts the summarization work and starts the embedding work and returns
immediately. Each half independently records that it is genuinely done — the last section summary to complete
records one flag, the embedding pass records the other once it finishes — and the library is marked ready the
moment both flags are present, checked atomically so there is no window where only one has landed. If the
application or a worker restarts mid-flight, nothing about this wait is lost, because nothing about it was
held in a process's memory to begin with — the whole thing is durable, queued state.

**A document whose summarization or a library whose embedding permanently fails moves the library to a
failed state, not an indefinite silent hold.** A summarization or embedding attempt that exhausts its retries
marks the library failed and records why, rather than only logging and leaving the library stuck pending
forever with no visible state change. Failed is a real, queryable status distinct from ingested or ready — a
stuck library is now something the system reports, not something an operator has to notice is simply never
finishing.

**The search index is disposable; the tree and tables are not.** Nothing about the content tree or the
canonical tables is specific to any embedding model — they are typed, structured, model-agnostic state, built
once by the ingestion pipeline and never touched by the prep phase. Embedding is a pure reduction *over* that
structure, run separately, after it. Swapping the embedding model, or the summarizing model, therefore means
re-running the prep phase against structure that already exists — not re-ingesting a single source file. The
expensive step (reading, layout, table structuring) is paid once, independent of which model reads the result
afterward.

The prep phase is gated by the library's **tier**. A *structure* library is ingested to the lossless tree and
tables and stops there; a *search* library additionally gets the summaries, embeddings and indexes that Part
II runs on. The tier is a pricing/access boundary, not a different pipeline — a structure library can be
upgraded and finalized later without re-ingesting. The phase is event-driven: it fires the moment a library's
last in-flight document drains, and is re-checked on restart for any library that became ready while the
application was down.

---

## Part II — Query

One model call resolves the question against the whole library at a uniform grain and returns what the
answer must account for and how deeply each of it must be read. Everything after that executes that
decision: the database computes every figure, and one deterministic budget rule fits both kinds of evidence
into a single answer.

The hard part is not finding candidates. It is deciding which of a large body of evidence actually bears
on the question, fitting exactly that into one answer, and never letting the model invent a figure or
miscount what fits.

## Query pipeline

```
                     ┌─→ tables at full ──→ query ──→ execute ─────────────┐
question ─→ resolve ─┤   (model over schema + samples)                     ├─→ synthesize → answer
 (inventory)         └─→ documents ──→ floor, then depth to an equal share ┘        (model)
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

### Sections — the unit of text relevance, built once in the prep phase

The reason pre-built section summaries exist is **amortization**. Deciding which prose bears on a question
requires reading prose, and reading a whole corpus per question is a map-reduce every time — paid again on
every query, over text that never changed. Summarizing once, up front, moves that cost off the query path
and makes it reusable: the corpus is read once, and every subsequent query judges relevance against the
stored result. Query-time map-reduce then becomes the fallback for evidence that genuinely exceeds the
window, not the routine path.

Headings are author-written labels. "Introduction" or "Section 3" carries no signal, so a channel that judges
relevance from headings alone judges from metadata rather than content. Tables have an organic compressed
form — schema and sample rows, the table's own content uninterpreted — but prose has none: sampled
paragraphs are not representative. This is the one place in the system where generated text is justified.

Each document is packed into **sections**: content blocks in document order are accumulated up to a size
limit, and each section is summarized once, in the **prep phase** — after the library's structure is
complete, not during ingestion — drained by the same shared, bounded-concurrency pool as every other
model-driven stage. Each section stores its own block range and page range at creation, so citation is a
stored field, not recomputed. Summary length is bounded per section, proportionate to its own content, so
the summaries stay a true reduction rather than growing to fill whatever room they're given. Summaries are a
*reduction over persisted structure*, not structure itself — which is why they run here, behind the same
tier gate as embedding, rather than streaming during ingestion where they contended with page reading and
could leave a query a partial inventory.

This gives the hierarchy the query side needs — document summary → section summary → block — and it means
relevance is judged against content, not labels. A document's own summary is written at the end of the same
pass, reduced from its section summaries once they all exist, to a fixed per-document ceiling: it is that
document's entry in the inventory, and the whole library's entries must fit one call.

### Resolution — coverage and depth, decided once

Membership is decided **once**, for the whole question, with every item visible at the same grain. Each
document appears as its summary; each table as its filename, where in the file it sits, its title or
caption, its row count and its column names — no data types, no sample values, because those scale with
width and this view exists to judge bearing, not to write a query. The model returns the items the answer
must account for, grouped by how deeply each must be read:

| depth | a document | a table |
|---|---|---|
| overall | what it covers as a whole | what it is about and what it holds |
| parts | what its individual relevant sections cover | — |
| full | the wording of those sections | values drawn or computed from its rows |

Two properties follow, and they are the point of the design. **Coverage is a set we can enforce**, not a
length we infer from how much some stage happened to return. And **the two kinds of evidence are judged
together**: a question about the collection can put a table in coverage at overall depth and a document at
parts depth in the same decision, which two independently-run channels could never agree on.

Only what is listed reaches the answer, so the prompt says exactly that — nothing downstream sees the
inventory, and an item left out contributes nothing however plainly it was described.

**Tables that need their rows.** Only those go to the query writer, and they arrive with full schema and
sample rows — the expensive view, bought once the set is small. The sample rows show how values are
*encoded* (one date format versus another, a numeric-looking value stored as text); a query written against a
schema alone can silently match nothing. The row count is carried at both steps, and it does two jobs: at
resolution it is how big a table is, which questions ask about directly; at writing it is what makes the
model aggregate a large table rather than ask for raw rows.

The writer no longer states relevance — it only writes queries, because every table it sees was already
resolved as needing its rows. A table that matters for *what it is* never reaches it, and is carried by its
description instead. There is no synthetic listing of the corpus's own shape offered to the writer: the row
counts and filenames are already in the inventory the resolver reads.

Coverage is decided structurally, from this compressed inventory, rather than by ranking passages against
the question. A ranking that has to decide inclusion over an entire corpus makes the broadest questions the
most expensive to answer well, because a broad question matches everything and a wide ranked sweep filters
nothing; deciding coverage from a compact inventory instead makes the broadest question the *cheapest* path,
not the most expensive one — every document goes into coverage and depth is decided afterward. There is no
ranking or filtering *within* a document to deepen it selectively today: a document is carried at its
summary, at the summaries of its parts, or at their full wording, chosen by the fitting rule below, not by a
relevance score.

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
exactly the rows asked for), and every result table is kept, reducing rows only if they collectively overflow
the remaining budget, with an explicit "showing some of the total rows" marker. Nothing is re-aggregated
after execution: the query already decided the shape. The genuine prevention is upstream — the writer sees
each row count and aggregates in the query itself when a raw unfiltered read would be huge. Rows come *after*
the floor because a result is an answer, and an answer may not crowd out what has to be accounted for.

**Depth takes what the rows leave, capped at an equal share per document.** Each covered document is carried
one rung deeper while its own share of the remaining budget allows: the depths resolution asked for have
first claim, and whatever is still spare carries the rest of the coverage from its summary to the summaries
of its parts — an unspent budget buys nothing. Each ask carries its own way down, so a document whose wording
will not fit lands on its parts rather than falling back to a one-line summary. Both rungs are priced from
what ingestion stored — content size for wording, summary size for parts — so an unaffordable rung is
rejected before anything is read.

The **equal share** is what keeps the answer proportionate. A long document's parts outnumber a short one's
many times over, and without a ceiling the longest document in the coverage would write most of the answer —
a very long volume beside a one-page document would otherwise dominate an answer that should weigh both. No
document may take more than its even share of what remains, and one that cannot fit its share stays at its
summary.

So depth is **measured, never classified**: real sizes against a real budget decide each rung, not a count
or a score. Membership is never cut — a document that cannot be shown at the depth it asked for is shown at
a shallower one, never dropped — and because every rung cites at page level or better, coverage is honest at
any depth.

### Arithmetic stays in the database

A sum, average, or count is computed by the database, never by a model reading rows. The result tables are
the answer; they are never reduced by re-aggregation. A join's cardinality is not predictable from its
inputs, so a near-cartesian blowup is discoverable only after execution and is usually a wrong key — worth
logging as a correctness signal, not just a budget event. Whenever rows are dropped to fit, it is marked, so
the answer says it is showing part of a larger total rather than presenting a partial as a total.

A single computed value carries no evidence of its own scope: a number under a model-chosen name reads the
same whether it is a grand total or filtered to one narrow slice, and synthesis, seeing only the number, will
disclaim or misattribute it. So each result is rendered with the **operation that produced it** — the
executed query, with its column references rewritten to header names rather than internal identifiers. This
is provenance for synthesis to read, not for the reader to see: it states what a figure means so a filtered
figure is never mistaken for the whole, and synthesis is told to state the figure plainly and never surface
the operation itself.

### Citation is structural and free

Citations do not depend on depth. A section shown at summary depth still cites at block and page level,
because the section stored its range when it was built; a block shown directly carries its own filename and
page; a document shown at its own summary cites the file. This is what makes shallow depth honest: a document
too broad to expand still contributes its real provenance.

What citation cannot check is *which rung a figure came from*. A number quoted out of a summary and a number
computed from rows cite identically, and the fabrication check — which compares cited files against evidence
files — passes both. A figure lifted from prose that happens to be wrong is therefore invisible to it.

### Synthesize

One model call merges the text evidence and the computed results. Computed results are the authority for
figures and totals; text supplies narrative and context; the model is where the two reconcile. If the
evidence does not answer the question, it says so plainly. It is told to account for each piece of evidence
once: many near-identical summaries from one long document are the condition under which a single
free-running call degenerates into repeating itself until it runs out of room.

### The one irreducible risk

Summary quality. Two summaries now stand between the query and the content, and they fail differently. A bad
**section** summary misrepresents a part of a document. A bad **document** summary is worse: it is the only
thing resolution ever sees of that document, so a document whose summary omits what it holds is not merely
under-read — it is never selected at all, and the failure is indistinguishable from a document that
genuinely bears on nothing. Both are set once, in the prep phase, and neither can be compensated downstream.

### Open

- **Corpus scale.** The inventory grows with the corpus — one summary per document, one entry per table —
  and resolution is deliberately **one** call, because splitting it would fragment the judgment it exists to
  make: each call would weigh how much of the collection the answer must account for against the slice it was
  given, never the whole, so the same question would be judged against a different denominator in every call
  and the union of their choices would be bounded by nothing. A per-item ceiling (a fixed document-summary
  length; identity and column names, never values, per table) keeps the inventory small, but the total is
  still linear in items, so past some corpus size, scoping needs its own retrieval step ahead of resolution.
  The same ceiling appears at synthesis, where a floor that overflows is merged. One library fits one call
  today, so this is deferred, not solved. Table entries carry a second term: their cost is linear in total
  *columns*, not tables, so a corpus of wide sheets reaches the ceiling sooner than a corpus of many narrow
  ones.

- **Resolution is document-grain, so within-document specificity is invisible to it.** Only document
  summaries are read when coverage is decided, so something named in exactly one section of one document —
  and not in that document's summary — cannot be routed to. The false negative is indistinguishable from a
  question the corpus genuinely has nothing on: both surface as empty coverage.

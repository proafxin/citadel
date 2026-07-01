# Citadel Architecture

How Citadel turns an uploaded file of *any* format into a **queryable relational knowledge layer** — a
lossless content tree plus canonical tables in a relational database — and then answers a question over it
by having the model **write SQL the database executes**, so figures are computed, never guessed. Part I
(ingestion) is built; Part II (query) is designed.

---

# Part I — Ingestion

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
per-upload script. Each phase is independent:

```
upload → normalize → paginate → ocr ─────────────────→ merge → relational store
                                  └─ gapfill (scanned) ──┘
                     └────────────→ tabular ───────────→ merge
```

| Phase | Work |
|---|---|
| normalize | route + convert to one of the three lanes |
| paginate | render PDF pages; or split markup/text/tabular into units |
| ocr | read each page visually → blocks |
| gapfill | scanned pages only: recover the lines the visual read dropped |
| merge | blocks → split paratext → stitch tables → structure embedded tables → content tree → persist |
| tabular | per sheet/file: structure + describe → canonical Table |

Bytes flow through the bus in memory; nothing but the final result is written to disk.

### Reading pages (ocr)

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
canonical shape: typed columns (each a header, a type, a role of *data* or *section*, and a unit where one
applies), the full rows, a sample, a description, its title/caption/notes, and provenance. The full rows
are **stored as relational rows and queried by SQL — never embedded.**

The rule that makes it trustworthy: **the model emits structure and descriptions, never data.** It sees
only anchors — the table's size, its top rows, and a few sampled rows — and returns the shape; every cell
is copied verbatim from the source and column types are inferred from the values. This is the same
boundary the query side relies on: the model writes the query, the database computes the values.

Tables are separated **by schema**: a run of rows with consistent columns is one table; a schema change
starts a new one, so a single source table yields **one or more** canonical tables — a stacked invoice
splits into its summary block and its line-items. A column whose values group the rows becomes a *section*
column instead of a new table.

By source:

- **Spreadsheets** — every cell, merge, table object and frozen pane is captured; contiguous regions are
  found and their structure resolved from anchors.
- **CSV/TSV** — types inferred deterministically; blank headers named from the column's own values. No
  model for structure.
- **JSON** — shredded deterministically into normalized, joinable tables: nested objects flattened by
  dotted key, lists of objects into child tables keyed back to their parent.
- **Embedded HTML/PDF tables** — the grid is built deterministically (spans expanded), then the same
  structure step the spreadsheets use resolves multi-row headers, the section column, and schema splits
  into one or more tables; types inferred from the cells. Tables continuing across consecutive pages are
  stitched into one first — fragments with a matching column count across a page boundary merged, repeated
  headers dropped, page furniture skipped — so a forty-page table is structured as one.

Every table gets a description written for retrieval — its subject, what a row represents, and the
entities and vocabulary a user would search for.

## Search representation

Citadel retrieves **leaves and tables, never chunks**. A leaf's search text is its own cleaned text plus
its context: library, filename, the headings above it, and the page's paratext; cleaning normalizes
Unicode, rejoins hyphen-split words, collapses whitespace, and turns machine names into words. A table's
search text is richer: library, file, sheet, title, caption, notes, its column headers, and its
description. Rows stay off the text path.

Search text is indexed two ways (**designed**): dense multilingual vectors for meaning, and a lexical
(BM25) index for exact names, identifiers and codes. A table is *found* through its search text and
*answered* by SQL over its rows.

## Storage

- A **streaming bus** holds the pipeline's in-flight state (bytes, page images, per-page blocks) —
  ephemeral, cleared after merge.
- A **relational database** holds the durable content tree, the tables and their rows, and the search
  indexes — this is what queries run against.
- An **object store** holds the assembled per-document tree for download.

## Reliability

- Delivery is at-least-once; a crashed run's pending work is reclaimed and reprocessed on restart.
- Recording a page is idempotent, so a redelivered page is a no-op.
- A single merge fires exactly once — when the last page or sheet of a document completes.
- Work that fails is retried a bounded number of times, then given up cleanly.
- Failures are scoped: a page that can't be read leaves a marker and the document still completes as
  *partial*; a whole-document failure is marked *failed*.

## Output

The result of ingestion is the materialized tree — sections and leaves with their content, and full
canonical table representations. Progress is a state per document:
`queued → normalizing → paginating → reading → done / partial / failed`.

---

# Part II — Query

**Designed, not yet built.** Runs over the persisted tree and tables: the model decides what to ask, the
database computes the numbers.

## Query pipeline

```
question → understand → retrieve ───────────→ filter → resolve ───────────────→ synthesize → cited report / chat
            (model)      text ‖ tables         (model)  model writes SQL,         (model)
                         dense + lexical → fused        database executes it
```

| Stage | Work |
|---|---|
| understand | reformulate for recall, translate to the corpus language, split into textual vs tabular parts |
| retrieve | search text and tables as separate channels, each dense + lexical, and fuse the rankings |
| filter | keep the candidate tables (columns + description, no rows) that can answer |
| resolve | the model writes one read-only query per table; the database executes it and returns exact values |
| synthesize | compose one cited answer from the text passages and the query results |

### Understand

One model call → the question plus one or two reformulations for recall, an optional translation into the
corpus language, and a split into the parts answered by text and the parts answered by tables.

### Retrieve

Two channels, each fused by reciprocal-rank fusion (rank-based, so text and tables need no comparable
scores):

- **Text** — each reformulation searched by meaning and lexically over leaf search text → fused → top
  leaves.
- **Tables** — each reformulation searched over table search text (description + columns + title/caption/
  notes) → fused → top candidate tables. Rows are never searched.

### Filter

The candidate tables' columns and descriptions, without any rows, go to the model, which keeps the ones
that can answer. This replaces a reranker, which can score how well a passage matches a query but not a
value a query has not yet produced. Skipped when retrieval already yields one or two clear tables.

### Resolve

For each kept table the model writes **one read-only query** against that table's columns, referring to
columns by position from a map of index to header, type, role and unit, and kept simple so it can't be
expensive. It runs against the table's rows through a typed projection that turns stored cells into typed,
named columns, inside a **read-only sandbox**: only SELECT, no other tables, a time limit and a row cap.
The model writes the query; the database computes the values.

### Synthesize

One model call merges the retrieved text passages, each with its document and page, and the query results,
each with its table and the query that produced it, into a single cited report or chat reply. Query
results are the authority for figures; text supplies context; there is no single numeric ranking across
modalities — the model is where the two reconcile. For a search interface that wants a ranked list of hits
instead of prose, the channel rankings are fused the same rank-based way.

## Models

One general model, run without a separate reasoning pass, performs every model step across ingestion and
query — table structuring and description, column naming, reformulation, filtering, query writing, and
synthesis. A vision model reads documents that exist only as pixels. A multilingual dense representation
carries meaning for search.

import { motion } from "motion/react";
import { useState } from "react";
import { DocumentRow } from "@/components/document-row";
import { Eyebrow } from "@/components/eyebrow";
import { Card } from "@/components/ui/card";
import type { DocumentItem, Library } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDuration } from "@/lib/format";

export type Bucket = "processing" | "ready" | "failed" | "skipped";
export type Filter = "all" | Bucket;

const PAGE_SIZE = 10;

const PILLS: { key: Filter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "processing", label: "Processing" },
  { key: "ready", label: "Ready" },
  { key: "failed", label: "Failed" },
  { key: "skipped", label: "Skipped" },
];

const DOT: Record<Bucket, string> = {
  processing: "bg-accent",
  ready: "bg-emerald-500",
  failed: "bg-red-400",
  skipped: "bg-ink-muted",
};

export function bucketOf(doc: DocumentItem): Bucket {
  switch (doc.status) {
    case "failed":
      return "failed";
    case "skipped":
      return "skipped";
    case "ingested":
    case "embedded":
    case "partial":
      return "ready";
    default:
      return "processing";
  }
}

export function hasActive(docs: DocumentItem[], searchable: boolean): boolean {
  return docs.some(
    (doc) => doc.status === "queued" || doc.status === "processing" || (searchable && doc.status === "ingested"),
  );
}

export function LibraryProgress({
  docs,
  library,
  filter,
  onFilter,
}: {
  docs: DocumentItem[];
  library: Library | undefined;
  filter: Filter;
  onFilter: (next: Filter) => void;
}) {
  const counts: Record<Bucket, number> = { processing: 0, ready: 0, failed: 0, skipped: 0 };
  for (const doc of docs) counts[bucketOf(doc)] += 1;
  const settled = counts.ready + counts.failed + counts.skipped;
  const overall = docs.length > 0 ? settled / docs.length : 0;
  const timings: string[] = [];
  if (library?.ingest_seconds != null) timings.push(`ingest ${formatDuration(library.ingest_seconds)}`);
  if (library?.total_seconds != null) timings.push(`total ${formatDuration(library.total_seconds)}`);

  return (
    <Card className="p-5">
      <div className="flex items-center justify-between">
        <Eyebrow>Progress</Eyebrow>
        <span className="text-xs text-ink-muted">
          {counts.ready}/{docs.length} ready
          {timings.length > 0 ? ` · ${timings.join(" · ")}` : ""}
        </span>
      </div>
      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-surface-2">
        <motion.div
          className="h-full rounded-full bg-accent"
          initial={false}
          animate={{ width: `${overall * 100}%` }}
          transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
        />
      </div>
      <div className="mt-4 flex flex-wrap gap-1.5">
        {PILLS.map((pill) => (
          <button
            key={pill.key}
            type="button"
            onClick={() => onFilter(pill.key)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium transition-colors",
              filter === pill.key ? "bg-surface-2 text-ink" : "text-ink-muted hover:bg-surface-2 hover:text-ink",
            )}
          >
            {pill.key !== "all" ? <span className={cn("size-1.5 rounded-full", DOT[pill.key])} /> : null}
            {pill.label} {pill.key === "all" ? docs.length : counts[pill.key]}
          </button>
        ))}
      </div>
    </Card>
  );
}

export function DocumentsList({
  libraryId,
  docs,
  filter,
  isLoading,
  isError,
  error,
}: {
  libraryId: number;
  docs: DocumentItem[];
  filter: Filter;
  isLoading: boolean;
  isError: boolean;
  error: unknown;
}) {
  const [page, setPage] = useState(0);
  const filtered = filter === "all" ? docs : docs.filter((doc) => bucketOf(doc) === filter);
  const lastPage = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const current = Math.min(page, lastPage - 1);
  const shown = filtered.slice(current * PAGE_SIZE, current * PAGE_SIZE + PAGE_SIZE);

  return (
    <div className="flex flex-col">
      <div className="flex h-6 items-center">
        <Eyebrow>All documents</Eyebrow>
      </div>
      <div className="mt-4 flex-1 space-y-2">
        {isLoading ? <p className="text-sm text-ink-muted">Loading…</p> : null}
        {isError ? (
          <Card className="border-red-500/30 p-5 text-sm text-red-400">{(error as Error).message}</Card>
        ) : null}
        {docs.length === 0 && !isLoading ? (
          <Card className="h-full p-6 text-sm text-ink-muted">No documents yet.</Card>
        ) : null}
        {shown.map((doc) => (
          <DocumentRow key={doc.id} doc={doc} libraryId={libraryId} />
        ))}
      </div>

      {filtered.length > PAGE_SIZE ? (
        <div className="mt-4 flex items-center justify-between text-sm text-ink-muted">
          <button
            type="button"
            disabled={current === 0}
            onClick={() => setPage(current - 1)}
            className="rounded-lg px-3 py-1.5 transition-colors hover:bg-surface-2 disabled:opacity-40"
          >
            Prev
          </button>
          <span>
            Page {current + 1} of {lastPage}
          </span>
          <button
            type="button"
            disabled={current >= lastPage - 1}
            onClick={() => setPage(current + 1)}
            className="rounded-lg px-3 py-1.5 transition-colors hover:bg-surface-2 disabled:opacity-40"
          >
            Next
          </button>
        </div>
      ) : null}
    </div>
  );
}

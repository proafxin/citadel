import { useQuery } from "@tanstack/react-query";
import { motion } from "motion/react";
import { useState } from "react";
import { DocumentRow, isInFlight } from "@/components/document-row";
import { DocumentUpload } from "@/components/document-upload";
import { Eyebrow } from "@/components/eyebrow";
import { Card } from "@/components/ui/card";
import { type DocumentItem, listDocuments } from "@/lib/api";
import { cn } from "@/lib/cn";

type Bucket = "processing" | "queued" | "ready" | "failed";
type Filter = "all" | Bucket;

const PAGE_SIZE = 10;
const LIVE_CAP = 5;

const PILLS: { key: Filter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "processing", label: "Processing" },
  { key: "queued", label: "Queued" },
  { key: "ready", label: "Ready" },
  { key: "failed", label: "Failed" },
];

function bucketOf(doc: DocumentItem): Bucket {
  const s = (doc.state ?? doc.status ?? "").toLowerCase();
  if (s === "done") return "ready";
  if (s.includes("fail")) return "failed";
  if (s === "queued" || s === "pending" || s === "") return "queued";
  return "processing";
}

export function DocumentsPanel({ libraryId }: { libraryId: number }) {
  const [filter, setFilter] = useState<Filter>("all");
  const [page, setPage] = useState(0);
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["documents", libraryId],
    queryFn: () => listDocuments(libraryId),
    refetchInterval: (query) => (query.state.data?.some(isInFlight) ? 2_000 : false),
  });

  const docs = data ?? [];
  const counts: Record<Bucket, number> = { processing: 0, queued: 0, ready: 0, failed: 0 };
  for (const doc of docs) counts[bucketOf(doc)] += 1;
  const totalPages = docs.reduce((sum, doc) => sum + (doc.page_count ?? 0), 0);
  const donePages = docs.reduce((sum, doc) => sum + (doc.done_count ?? 0), 0);
  const overall = totalPages > 0 ? donePages / totalPages : docs.length > 0 ? counts.ready / docs.length : 0;

  const live = docs.filter((doc) => bucketOf(doc) === "processing").slice(0, LIVE_CAP);
  const showStrip = live.length > 0 && filter !== "processing";
  const filtered = filter === "all" ? docs : docs.filter((doc) => bucketOf(doc) === filter);
  const lastPage = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const current = Math.min(page, lastPage - 1);
  const shown = filtered.slice(current * PAGE_SIZE, current * PAGE_SIZE + PAGE_SIZE);

  function pick(key: Filter) {
    setFilter(key);
    setPage(0);
  }

  return (
    <section>
      {docs.length > 0 ? (
        <Card className="p-5">
          <div className="flex items-center justify-between">
            <Eyebrow>Progress</Eyebrow>
            <span className="text-xs text-ink-muted">
              {counts.ready}/{docs.length} ready
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
                onClick={() => pick(pill.key)}
                className={cn(
                  "rounded-full px-2.5 py-1 text-xs font-medium transition-colors",
                  filter === pill.key ? "bg-accent text-accent-ink" : "bg-surface-2 text-ink-muted hover:text-ink",
                )}
              >
                {pill.label} {pill.key === "all" ? docs.length : counts[pill.key]}
              </button>
            ))}
          </div>
        </Card>
      ) : null}

      <div className="mt-4">
        <DocumentUpload libraryId={libraryId} />
      </div>

      {showStrip ? (
        <div className="mt-5 rounded-xl border border-accent/30 bg-accent/5 p-4">
          <div className="flex items-center gap-2">
            <span className="size-1.5 animate-pulse rounded-full bg-accent" />
            <Eyebrow>Now processing · live</Eyebrow>
          </div>
          <div className="mt-3 space-y-2">
            {live.map((doc) => (
              <DocumentRow key={doc.id} doc={doc} libraryId={libraryId} />
            ))}
          </div>
        </div>
      ) : null}

      <div className="mt-6">
        <Eyebrow>All documents</Eyebrow>
        <div className="mt-2 space-y-2">
          {isLoading ? <p className="text-sm text-ink-muted">Loading…</p> : null}
          {isError ? (
            <Card className="border-red-500/30 p-5 text-sm text-red-400">{(error as Error).message}</Card>
          ) : null}
          {docs.length === 0 && !isLoading ? (
            <Card className="p-6 text-sm text-ink-muted">No documents yet.</Card>
          ) : null}
          {shown.map((doc) => (
            <DocumentRow key={doc.id} doc={doc} libraryId={libraryId} />
          ))}
        </div>
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
    </section>
  );
}

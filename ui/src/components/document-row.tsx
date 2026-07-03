import { Link } from "@tanstack/react-router";
import { motion } from "motion/react";
import { StatusBadge } from "@/components/status-badge";
import { Card } from "@/components/ui/card";
import type { DocumentItem } from "@/lib/api";
import { formatDuration } from "@/lib/format";

const VIEWABLE = new Set(["ingested", "embedded", "partial"]);

export function isPending(doc: DocumentItem): boolean {
  return doc.status === "pending";
}

export function isIngested(doc: DocumentItem): boolean {
  return doc.status === "ingested";
}

export function DocumentRow({ doc, libraryId }: { doc: DocumentItem; libraryId: number }) {
  const active = isPending(doc);
  const viewable = VIEWABLE.has(doc.status);
  const total = doc.page_count ?? 0;
  const fraction = active && total > 0 ? Math.min(1, (doc.done_count ?? 0) / total) : 0;

  const inner = (
    <>
      <div className="flex items-center justify-between gap-4">
        <span className="truncate font-mono text-sm text-ink">{doc.filename}</span>
        <div className="flex shrink-0 items-center gap-3">
          {doc.elapsed != null ? (
            <span className="text-xs tabular-nums text-ink-muted">{formatDuration(doc.elapsed)}</span>
          ) : null}
          <StatusBadge doc={doc} />
        </div>
      </div>
      {active ? (
        <div className="mt-2.5 h-1 overflow-hidden rounded-full bg-surface-2">
          {total > 0 ? (
            <motion.div
              className="h-full rounded-full bg-accent"
              initial={false}
              animate={{ width: `${fraction * 100}%` }}
              transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
            />
          ) : (
            <div className="h-full w-1/3 animate-pulse rounded-full bg-accent/60" />
          )}
        </div>
      ) : null}
    </>
  );

  if (viewable) {
    return (
      <Link
        to="/library/$libraryId/document/$docId"
        params={{ libraryId: String(libraryId), docId: String(doc.id) }}
        className="block"
      >
        <Card className="px-4 py-3 transition-colors hover:border-ink-muted/40">{inner}</Card>
      </Link>
    );
  }
  return <Card className="px-4 py-3">{inner}</Card>;
}

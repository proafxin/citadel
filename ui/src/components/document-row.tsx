import { Link } from "@tanstack/react-router";
import { motion } from "motion/react";
import { StatusBadge } from "@/components/status-badge";
import { Card } from "@/components/ui/card";
import type { DocumentItem } from "@/lib/api";

function state(doc: DocumentItem): string {
  return (doc.state ?? doc.status ?? "").toLowerCase();
}

export function isInFlight(doc: DocumentItem): boolean {
  const s = state(doc);
  return s !== "done" && !s.includes("fail");
}

export function isDone(doc: DocumentItem): boolean {
  return state(doc) === "done";
}

export function DocumentRow({ doc, libraryId }: { doc: DocumentItem; libraryId: number }) {
  const active = isInFlight(doc);
  const done = state(doc) === "done";
  const total = doc.page_count ?? 0;
  const fraction = active && total > 0 ? Math.min(1, (doc.done_count ?? 0) / total) : 0;

  const inner = (
    <>
      <div className="flex items-center justify-between gap-4">
        <span className="truncate font-mono text-sm text-ink">{doc.filename}</span>
        <StatusBadge doc={doc} />
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

  if (done) {
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

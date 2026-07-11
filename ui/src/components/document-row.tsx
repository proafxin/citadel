import { StatusBadge } from "@/components/status-badge";
import { Card } from "@/components/ui/card";
import { type DocState, isSettled, isViewable } from "@/lib/doc-state";
import { formatDuration } from "@/lib/format";
import { Link } from "@tanstack/react-router";

export function DocumentRow({ doc, libraryId }: { doc: DocState; libraryId: number }) {
  const active = !isSettled(doc);
  const pct = doc.total > 0 ? Math.min(100, Math.round((doc.done / doc.total) * 100)) : 0;

  const inner = (
    <>
      <div className="flex items-center justify-between gap-4">
        <span className="truncate font-mono text-sm text-ink">{doc.filename}</span>
        <div className="flex shrink-0 items-center gap-3">
          {doc.elapsed != null ? (
            <span className="text-xs tabular-nums text-ink-muted">{formatDuration(doc.elapsed)}</span>
          ) : null}
          <StatusBadge status={doc.status} />
        </div>
      </div>
      {active ? (
        <div className="mt-2.5 h-1 overflow-hidden rounded-full bg-surface-2">
          {doc.total > 0 ? (
            <div className="h-full rounded-full bg-accent transition-all duration-500" style={{ width: `${pct}%` }} />
          ) : (
            <div className="h-full w-1/3 animate-pulse rounded-full bg-accent/60" />
          )}
        </div>
      ) : null}
    </>
  );

  if (isViewable(doc)) {
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

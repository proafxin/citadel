import type { DocState } from "@/lib/doc-state";

export function NowProcessing({ states }: { states: DocState[] }) {
  const live = states.filter((doc) => doc.status === "processing");
  const queued = states.filter((doc) => doc.status === "queued");
  if (live.length === 0 && queued.length === 0) return null;

  return (
    <div className="space-y-3 rounded-xl border border-accent/30 bg-accent/5 p-4">
      {live.map((doc) => {
        const pct = doc.total > 0 ? Math.min(100, Math.round((doc.done / doc.total) * 100)) : 0;
        return (
          <div key={doc.id} className="space-y-1.5">
            <div className="flex items-center justify-between gap-3">
              <span className="truncate font-mono text-xs text-ink">{doc.filename}</span>
              <span className="shrink-0 tabular-nums text-xs text-ink-muted">
                {doc.done}/{doc.total} pages{doc.active > 0 ? ` · ${doc.active} in progress` : ""}
              </span>
            </div>
            <div className="h-1 overflow-hidden rounded-full bg-surface-2">
              <div className="h-full rounded-full bg-accent transition-all duration-500" style={{ width: `${pct}%` }} />
            </div>
          </div>
        );
      })}
      {queued.length > 0 && (
        <div className="space-y-1 border-t border-accent/20 pt-2">
          {queued.map((doc) => (
            <div key={doc.id} className="flex items-center justify-between gap-3">
              <span className="truncate font-mono text-xs text-ink-muted">{doc.filename}</span>
              <span className="shrink-0 text-xs text-ink-muted">Queued</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

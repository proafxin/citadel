import { useQuery } from "@tanstack/react-query";
import { getProgress } from "@/lib/api";

export function NowProcessing({ libraryId }: { libraryId: number }) {
  const { data } = useQuery({
    queryKey: ["progress", libraryId],
    queryFn: () => getProgress(libraryId),
    refetchInterval: 1500,
  });
  const live = data ?? [];
  if (live.length === 0) return null;
  return (
    <div className="space-y-3 rounded-xl border border-accent/30 bg-accent/5 p-4">
      {live.map((p) => {
        const pct = p.total > 0 ? Math.min(100, Math.round((p.done / p.total) * 100)) : 0;
        return (
          <div key={p.doc_id} className="space-y-1.5">
            <div className="flex items-center justify-between gap-3">
              <span className="truncate font-mono text-xs text-ink">{p.filename}</span>
              <span className="shrink-0 tabular-nums text-xs text-ink-muted">
                {p.total > 0 ? `${p.done}/${p.total}` : p.state}
              </span>
            </div>
            <div className="h-1 overflow-hidden rounded-full bg-surface-2">
              <div className="h-full rounded-full bg-accent transition-all duration-500" style={{ width: `${pct}%` }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

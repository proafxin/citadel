import type { DocumentItem } from "@/lib/api";
import { cn } from "@/lib/cn";

const STATE_LABEL: Record<string, string> = {
  queued: "Queued",
  pending: "Queued",
  normalizing: "Processing",
  paginating: "Processing",
  done: "Ready",
  failed: "Failed",
};

export function StatusBadge({ doc }: { doc: DocumentItem }) {
  const raw = (doc.state ?? doc.status ?? "").toLowerCase();
  const done = raw === "done";
  const failed = raw.includes("fail");
  const label = done ? "Ready" : failed ? "Failed" : (STATE_LABEL[raw] ?? "Processing");
  const count =
    !done && !failed && doc.page_count && doc.page_count > 0 ? `${doc.done_count ?? 0}/${doc.page_count}` : null;

  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium",
        done && "bg-emerald-500/10 text-emerald-500",
        failed && "bg-red-500/10 text-red-400",
        !done && !failed && "bg-accent/10 text-accent",
      )}
    >
      <span
        className={cn(
          "size-1.5 rounded-full",
          done && "bg-emerald-500",
          failed && "bg-red-400",
          !done && !failed && "animate-pulse bg-accent",
        )}
      />
      {label}
      {count ? ` · ${count}` : ""}
    </span>
  );
}

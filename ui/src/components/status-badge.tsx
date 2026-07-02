import type { DocumentItem } from "@/lib/api";
import { cn } from "@/lib/cn";

export function StatusBadge({ doc }: { doc: DocumentItem }) {
  const label = doc.state ?? doc.status ?? "unknown";
  const raw = label.toLowerCase();
  const done = raw === "done";
  const failed = raw.includes("fail");
  const progress =
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
      {progress ? ` · ${progress}` : ""}
    </span>
  );
}

import { cn } from "@/lib/cn";
import { tierMeta } from "@/lib/tiers";

export function TierBadge({ tier, className }: { tier: string; className?: string }) {
  const meta = tierMeta(tier);
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium",
        meta.searchable ? "border-accent/40 text-accent" : "border-border text-ink-muted",
        className,
      )}
    >
      <span className={cn("size-1.5 rounded-full", meta.searchable ? "bg-accent" : "bg-ink-muted")} />
      {meta.label}
    </span>
  );
}

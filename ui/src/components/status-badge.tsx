import type { DocumentItem } from "@/lib/api";
import { cn } from "@/lib/cn";

const LABELS: Record<string, string> = {
  pending: "Processing",
  ingested: "Ready",
  embedded: "Ready",
  partial: "Partial",
  failed: "Failed",
  skipped: "Skipped",
};

type Tone = "ready" | "warn" | "bad" | "muted" | "active";

function toneFor(key: string): Tone {
  switch (key) {
    case "ingested":
    case "embedded":
      return "ready";
    case "partial":
      return "warn";
    case "failed":
      return "bad";
    case "skipped":
      return "muted";
    default:
      return "active";
  }
}

const TONE_PILL: Record<Tone, string> = {
  ready: "bg-emerald-500/10 text-emerald-500",
  warn: "bg-amber-500/10 text-amber-500",
  bad: "bg-red-500/10 text-red-400",
  muted: "bg-surface-2 text-ink-muted",
  active: "bg-accent/10 text-accent",
};

const TONE_DOT: Record<Tone, string> = {
  ready: "bg-emerald-500",
  warn: "bg-amber-500",
  bad: "bg-red-400",
  muted: "bg-ink-muted",
  active: "animate-pulse bg-accent",
};

export function StatusBadge({ doc }: { doc: DocumentItem }) {
  const key = doc.status.toLowerCase();
  const tone = toneFor(key);
  const label = LABELS[key] ?? "Processing";

  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium",
        TONE_PILL[tone],
      )}
    >
      <span className={cn("size-1.5 rounded-full", TONE_DOT[tone])} />
      {label}
    </span>
  );
}

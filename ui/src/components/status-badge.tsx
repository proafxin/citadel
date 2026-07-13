import { cn } from "@/lib/cn";
import type { Status } from "@/lib/doc-state";

const LABELS: Record<Status, string> = {
  queued: "Queued",
  processing: "Processing",
  ingested: "Ingested",
  embedded: "Embedded",
  partial: "Partial",
  failed: "Failed",
  skipped: "Skipped",
};

type Tone = "ready" | "warn" | "bad" | "muted" | "active";

const TONES: Record<Status, Tone> = {
  queued: "muted",
  processing: "active",
  ingested: "ready",
  embedded: "ready",
  partial: "warn",
  failed: "bad",
  skipped: "muted",
};

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

export function StatusBadge({ status }: { status: Status }) {
  const tone = TONES[status];

  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium",
        TONE_PILL[tone],
      )}
    >
      <span className={cn("size-1.5 rounded-full", TONE_DOT[tone])} />
      {LABELS[status]}
    </span>
  );
}

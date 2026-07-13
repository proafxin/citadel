import type { DocProgress, DocumentItem } from "@/lib/api";

export type Status = "queued" | "processing" | "ingested" | "embedded" | "partial" | "failed" | "skipped";
export type Bucket = "queued" | "processing" | "ready" | "failed" | "skipped";

export type DocState = {
  id: number;
  filename: string;
  status: Status;
  done: number;
  active: number;
  total: number;
  elapsed: number | null;
};

const SETTLED: Record<string, Status> = {
  ingested: "ingested",
  embedded: "embedded",
  partial: "partial",
  failed: "failed",
  skipped: "skipped",
};

export function interpret(docs: DocumentItem[], progress: DocProgress[]): DocState[] {
  const byId = new Map(progress.map((row) => [row.doc_id, row]));
  return docs.map((doc) => {
    const live = byId.get(doc.id);
    const started = live != null && (live.done > 0 || live.active > 0);
    return {
      id: doc.id,
      filename: doc.filename,
      status: SETTLED[doc.status] ?? (started ? "processing" : "queued"),
      done: live?.done ?? 0,
      active: live?.active ?? 0,
      total: live?.total ?? 0,
      elapsed: doc.elapsed,
    };
  });
}

const BUCKET: Record<Status, Bucket> = {
  queued: "queued",
  processing: "processing",
  ingested: "ready",
  embedded: "ready",
  partial: "ready",
  failed: "failed",
  skipped: "skipped",
};

export function bucketOf(state: DocState): Bucket {
  return BUCKET[state.status];
}

export function isSettled(state: DocState): boolean {
  return state.status !== "queued" && state.status !== "processing";
}

export function isViewable(state: DocState): boolean {
  return state.status === "ingested" || state.status === "embedded" || state.status === "partial";
}

export function hasActive(states: DocState[]): boolean {
  return states.some((state) => !isSettled(state));
}

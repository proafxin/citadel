export type Tier = "tier_1" | "tier_2";

export type Library = {
  id: number;
  name: string;
  tier: string;
  status: string;
  ingest_started_at: string | null;
  ingested_at: string | null;
  finalize_started_at: string | null;
  described_at: string | null;
  embed_started_at: string | null;
  ready_at: string | null;
  ingest_seconds: number | null;
  describe_seconds: number | null;
  embed_seconds: number | null;
  finalize_seconds: number | null;
  total_seconds: number | null;
};

export type DocumentItem = {
  id: number;
  filename: string;
  status: string;
  elapsed: number | null;
};

export type DocProgress = {
  doc_id: number;
  filename: string;
  status: string;
  done: number;
  active: number;
  total: number;
};

const BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export function listLibraries(): Promise<Library[]> {
  return fetch(`${BASE}/libraries`).then((r) => json<Library[]>(r));
}

export function getLibrary(id: number): Promise<Library> {
  return fetch(`${BASE}/libraries/${id}`).then((r) => json<Library>(r));
}

export function createLibrary(name: string, tier: Tier): Promise<Library> {
  return fetch(`${BASE}/libraries`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, tier }),
  }).then((r) => json<Library>(r));
}

export function updateLibrary(id: number, name: string, tier: Tier): Promise<Library> {
  return fetch(`${BASE}/libraries/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, tier }),
  }).then((r) => json<Library>(r));
}

export async function deleteLibrary(id: number): Promise<void> {
  const res = await fetch(`${BASE}/libraries/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error(await res.text());
}

export type Config = {
  ingest_concurrency: number;
};

export function getConfig(): Promise<Config> {
  return fetch(`${BASE}/config`).then((r) => json<Config>(r));
}

export function listDocuments(libraryId: number): Promise<DocumentItem[]> {
  return fetch(`${BASE}/libraries/${libraryId}/documents`).then((r) => json<DocumentItem[]>(r));
}

export function uploadDocuments(libraryId: number, files: File[]): Promise<{ doc_ids: number[] }> {
  const form = new FormData();
  for (const file of files) form.append("files", file);
  return fetch(`${BASE}/libraries/${libraryId}/documents`, { method: "POST", body: form }).then((r) =>
    json<{ doc_ids: number[] }>(r),
  );
}

export function getResult(docId: number): Promise<unknown> {
  return fetch(`${BASE}/result/${docId}`).then((r) => json<unknown>(r));
}

export function getProgress(libraryId: number): Promise<DocProgress[]> {
  return fetch(`${BASE}/libraries/${libraryId}/progress`).then((r) => json<DocProgress[]>(r));
}

export function treeDownloadUrl(libraryId: number): string {
  return `${BASE}/libraries/${libraryId}/tree`;
}

export function exportUrl(libraryId: number): string {
  return `${BASE}/libraries/${libraryId}/export`;
}

export function queryStream(libraryId: number, question: string): Promise<Response> {
  return fetch(`${BASE}/query`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, library_id: libraryId }),
  });
}

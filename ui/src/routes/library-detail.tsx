import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { ArrowLeft, ArrowUp, Download, MessageSquare } from "lucide-react";
import { useState } from "react";
import { isIngested, isPending } from "@/components/document-row";
import { DocumentsList, type Filter, hasActive, LibraryProgress } from "@/components/documents-panel";
import { DocumentUpload } from "@/components/document-upload";
import { Eyebrow } from "@/components/eyebrow";
import { NowProcessing } from "@/components/now-processing";
import { TierBadge } from "@/components/tier-badge";
import { Card } from "@/components/ui/card";
import { exportUrl, getLibrary, listDocuments, updateLibrary } from "@/lib/api";
import { tierMeta } from "@/lib/tiers";

export function LibraryDetailPage() {
  const { libraryId } = useParams({ from: "/library/$libraryId" });
  const id = Number(libraryId);
  const qc = useQueryClient();
  const [filter, setFilter] = useState<Filter>("all");
  const libQ = useQuery({ queryKey: ["library", id], queryFn: () => getLibrary(id) });
  const meta = libQ.data ? tierMeta(libQ.data.tier) : null;
  const searchable = meta?.searchable ?? false;
  const docsQ = useQuery({
    queryKey: ["documents", id],
    queryFn: () => listDocuments(id),
    refetchInterval: (query) => (hasActive(query.state.data ?? [], searchable) ? 2_000 : false),
  });
  const upgrade = useMutation({
    mutationFn: () => updateLibrary(id, libQ.data?.name ?? "", "tier_2"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["library", id] });
      qc.invalidateQueries({ queryKey: ["documents", id] });
    },
  });
  const docs = docsQ.data ?? [];
  const processing = docs.some(isPending);
  const ingested = docs.length > 0 && !processing;
  const ready = searchable && docs.length > 0 && !docs.some((doc) => isPending(doc) || isIngested(doc));

  return (
    <div>
      <Link
        to="/"
        className="inline-flex items-center gap-1.5 text-sm text-ink-muted transition-colors hover:text-ink"
      >
        <ArrowLeft size={15} /> Libraries
      </Link>

      <div className="mt-4 grid gap-6 lg:grid-cols-[1fr_2fr] lg:items-start">
        <div>
          <Eyebrow>Library</Eyebrow>
          <div className="mt-2 flex flex-wrap items-center gap-4">
            <h1 className="font-display text-4xl tracking-tight text-ink">{libQ.data?.name ?? "…"}</h1>
            {libQ.data ? <TierBadge tier={libQ.data.tier} /> : null}
            {ingested ? (
              <a
                href={exportUrl(id)}
                className="inline-flex h-9 items-center gap-2 rounded-lg border border-border px-3 text-sm font-medium text-ink transition-colors hover:bg-surface-2"
              >
                <Download size={15} /> Export
              </a>
            ) : (
              <span className="inline-flex h-9 cursor-not-allowed items-center gap-2 rounded-lg border border-border px-3 text-sm font-medium text-ink-muted opacity-50">
                <Download size={15} /> Export
              </span>
            )}
          </div>
        </div>
        <DocumentUpload libraryId={id} />
      </div>

      {docs.length > 0 ? (
        <div className="mt-6">
          <LibraryProgress docs={docs} filter={filter} onFilter={setFilter} />
        </div>
      ) : null}

      <div className="mt-8 grid gap-6 lg:grid-cols-2">
        <DocumentsList
          key={filter}
          libraryId={id}
          docs={docs}
          filter={filter}
          isLoading={docsQ.isLoading}
          isError={docsQ.isError}
          error={docsQ.error}
        />

        <aside className="flex flex-col">
          <div className="flex h-6 items-center gap-2">
            {processing ? <span className="size-1.5 animate-pulse rounded-full bg-accent" /> : null}
            <Eyebrow>{processing ? "Now processing" : "Ask"}</Eyebrow>
          </div>
          <div className="mt-4 flex-1">
            {processing ? (
              <NowProcessing libraryId={id} docs={docs} />
            ) : (
              <Card className="flex h-full flex-col p-6 text-sm text-ink-muted">
                {!meta?.searchable ? (
                  <div>
                    <p>Search is a Tier 2 feature.</p>
                    <button
                      type="button"
                      onClick={() => upgrade.mutate()}
                      disabled={upgrade.isPending || !libQ.data}
                      className="mt-4 inline-flex h-10 items-center gap-2 rounded-lg bg-accent px-4 text-sm font-medium text-accent-ink transition-colors hover:bg-accent-hover disabled:opacity-50"
                    >
                      <ArrowUp size={16} /> {upgrade.isPending ? "Upgrading…" : "Upgrade to Search"}
                    </button>
                  </div>
                ) : ready ? (
                  <Link
                    to="/library/$libraryId/ask"
                    params={{ libraryId: String(id) }}
                    className="inline-flex h-10 items-center gap-2 rounded-lg bg-accent px-4 text-sm font-medium text-accent-ink transition-colors hover:bg-accent-hover"
                  >
                    <MessageSquare size={16} /> Open chatbot
                  </Link>
                ) : (
                  <p>Available once all documents finish processing.</p>
                )}
              </Card>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}

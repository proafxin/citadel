import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { ArrowLeft, Download, MessageSquare } from "lucide-react";
import { isInFlight } from "@/components/document-row";
import { DocumentsPanel } from "@/components/documents-panel";
import { Eyebrow } from "@/components/eyebrow";
import { TierBadge } from "@/components/tier-badge";
import { Card } from "@/components/ui/card";
import { getLibrary, listDocuments, treeDownloadUrl } from "@/lib/api";
import { tierMeta } from "@/lib/tiers";

export function LibraryDetailPage() {
  const { libraryId } = useParams({ from: "/library/$libraryId" });
  const id = Number(libraryId);
  const libQ = useQuery({ queryKey: ["library", id], queryFn: () => getLibrary(id) });
  const docsQ = useQuery({ queryKey: ["documents", id], queryFn: () => listDocuments(id) });
  const meta = libQ.data ? tierMeta(libQ.data.tier) : null;
  const docs = docsQ.data ?? [];
  const ingested = docs.length > 0 && !docs.some(isInFlight);

  return (
    <div>
      <Link
        to="/"
        className="inline-flex items-center gap-1.5 text-sm text-ink-muted transition-colors hover:text-ink"
      >
        <ArrowLeft size={15} /> Libraries
      </Link>

      <div className="mt-4">
        <Eyebrow>Library</Eyebrow>
        <div className="mt-2 flex flex-wrap items-center gap-4">
          <h1 className="font-display text-4xl tracking-tight text-ink">{libQ.data?.name ?? "…"}</h1>
          {ingested ? (
            <a
              href={treeDownloadUrl(id)}
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

      <div className="mt-6 grid gap-6 lg:grid-cols-[1.6fr_1fr] lg:items-start">
        <div>
          <div className="flex h-6 items-center">{libQ.data ? <TierBadge tier={libQ.data.tier} /> : null}</div>
          <div className="mt-4">
            <DocumentsPanel libraryId={id} />
          </div>
        </div>

        <aside>
          <div className="flex h-6 items-center">
            <Eyebrow>Ask</Eyebrow>
          </div>
          <Card className="mt-4 p-6 text-sm text-ink-muted">
            {!meta?.searchable ? (
              <p>Search is a Tier 2 feature. Upgrade to enable chat.</p>
            ) : ingested ? (
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
        </aside>
      </div>
    </div>
  );
}

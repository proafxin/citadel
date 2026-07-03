import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { ArrowLeft, Download, MessageSquare } from "lucide-react";
import { DocumentsPanel } from "@/components/documents-panel";
import { Eyebrow } from "@/components/eyebrow";
import { TierBadge } from "@/components/tier-badge";
import { Card } from "@/components/ui/card";
import { getLibrary, treeDownloadUrl } from "@/lib/api";
import { tierMeta } from "@/lib/tiers";

export function LibraryDetailPage() {
  const { libraryId } = useParams({ from: "/library/$libraryId" });
  const id = Number(libraryId);
  const libQ = useQuery({ queryKey: ["library", id], queryFn: () => getLibrary(id) });
  const meta = libQ.data ? tierMeta(libQ.data.tier) : null;

  return (
    <div>
      <Link
        to="/"
        className="inline-flex items-center gap-1.5 text-sm text-ink-muted transition-colors hover:text-ink"
      >
        <ArrowLeft size={15} /> Libraries
      </Link>

      <div className="mt-4 flex flex-wrap items-end justify-between gap-4">
        <div>
          <Eyebrow>Library</Eyebrow>
          <h1 className="mt-2 font-display text-4xl tracking-tight text-ink">{libQ.data?.name ?? "…"}</h1>
          <div className="mt-3">{libQ.data ? <TierBadge tier={libQ.data.tier} /> : null}</div>
        </div>
        <a
          href={treeDownloadUrl(id)}
          className="inline-flex h-10 items-center gap-2 rounded-lg border border-border px-4 text-sm font-medium text-ink transition-colors hover:bg-surface-2"
        >
          <Download size={16} /> Export
        </a>
      </div>

      <div className="mt-8 grid gap-6 lg:grid-cols-[1.6fr_1fr]">
        <DocumentsPanel libraryId={id} />

        <aside>
          <Eyebrow>Ask</Eyebrow>
          <Card className="mt-3 p-6 text-sm text-ink-muted">
            {meta?.searchable ? (
              <Link
                to="/library/$libraryId/ask"
                params={{ libraryId: String(id) }}
                className="inline-flex h-10 items-center gap-2 rounded-lg bg-accent px-4 text-sm font-medium text-accent-ink transition-colors hover:bg-accent-hover"
              >
                <MessageSquare size={16} /> Open chatbot
              </Link>
            ) : (
              <p>Search is a Tier 2 feature. Upgrade to enable chat.</p>
            )}
          </Card>
        </aside>
      </div>
    </div>
  );
}

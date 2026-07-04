import { useQuery } from "@tanstack/react-query";
import { DocumentRow, isPending } from "@/components/document-row";
import { type DocumentItem, getConfig } from "@/lib/api";

export function NowProcessing({ libraryId, docs }: { libraryId: number; docs: DocumentItem[] }) {
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: getConfig, staleTime: Number.POSITIVE_INFINITY });
  const live = docs.filter(isPending).slice(0, config?.ingest_concurrency ?? 1);
  if (live.length === 0) return null;
  return (
    <div className="space-y-2 rounded-xl border border-accent/30 bg-accent/5 p-4">
      {live.map((doc) => (
        <DocumentRow key={doc.id} doc={doc} libraryId={libraryId} />
      ))}
    </div>
  );
}

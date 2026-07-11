import { Eyebrow } from "@/components/eyebrow";
import { Card } from "@/components/ui/card";
import { getResult } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { ArrowLeft } from "lucide-react";

type ListItem = { content?: string; depth?: number };

type TreeNode = {
  type: string;
  level?: number;
  label?: string;
  content?: string | null;
  list_items?: ListItem[] | null;
  columns?: unknown[];
  sample_rows?: unknown[][];
  n_rows?: number;
  description?: string | null;
  children?: TreeNode[];
  filename?: string;
};

function colHeader(col: unknown): string {
  if (col && typeof col === "object") {
    const o = col as Record<string, unknown>;
    if ("header" in o) return String(o.header);
    if ("name" in o) return String(o.name);
  }
  return String(col);
}

function TableView({ node }: { node: TreeNode }) {
  const headers = (node.columns ?? []).map(colHeader);
  const rows = node.sample_rows ?? [];
  return (
    <figure className="my-6 overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-y border-ink/25">
            {headers.map((h, i) => (
              <th key={`h-${i}`} className="px-3 py-2 text-left font-medium text-ink">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, ri) => (
            <tr key={`r-${ri}`} className="border-b border-border">
              {row.map((cell, ci) => (
                <td key={`c-${ri}-${ci}`} className="px-3 py-2 align-top text-ink-muted">
                  {cell == null ? "" : String(cell)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {node.description || node.n_rows ? (
        <figcaption className="mt-2 text-xs text-ink-muted">
          {node.description ? `${node.description} ` : ""}
          {node.n_rows ? `· ${rows.length} of ${node.n_rows} rows shown` : ""}
        </figcaption>
      ) : null}
    </figure>
  );
}

function NodeView({ node }: { node: TreeNode }) {
  const children = node.children ?? [];
  const kids = children.map((child, i) => <NodeView key={`n-${i}`} node={child} />);

  switch (node.type) {
    case "level": {
      const level = node.level ?? 1;
      const size = level <= 1 ? "text-2xl" : level === 2 ? "text-xl" : "text-lg";
      return (
        <section className="mt-7">
          <h2 className={cn("font-display text-ink", size)}>{node.label}</h2>
          {children.length ? <div className="mt-2">{kids}</div> : null}
        </section>
      );
    }
    case "table":
      return (
        <>
          <TableView node={node} />
          {kids}
        </>
      );
    case "list":
      return (
        <ul className="my-3 space-y-1 text-ink-muted">
          {(node.list_items ?? []).map((it, i) => (
            <li key={`li-${i}`} className="flex gap-2" style={{ marginLeft: (it.depth ?? 0) * 14 }}>
              <span className="text-ink-muted/60">·</span>
              <span>{it.content}</span>
            </li>
          ))}
          {kids}
        </ul>
      );
    case "code":
      return (
        <pre className="my-4 overflow-x-auto rounded-lg border border-border bg-surface p-3 font-mono text-xs text-ink">
          {node.content}
        </pre>
      );
    case "equation":
      return (
        <div className="my-4 rounded-lg border border-border bg-surface p-3 text-center font-mono text-sm text-ink">
          {node.content}
        </div>
      );
    default:
      return (
        <>
          {node.content ? (
            <p className="my-2 whitespace-pre-wrap leading-relaxed text-ink-muted">{node.content}</p>
          ) : null}
          {kids}
        </>
      );
  }
}

export function DocumentViewerPage() {
  const { libraryId, docId } = useParams({ from: "/library/$libraryId/document/$docId" });
  const q = useQuery({ queryKey: ["result", docId], queryFn: () => getResult(Number(docId)) });
  const tree = q.data as TreeNode | undefined;

  return (
    <div>
      <Link
        to="/library/$libraryId"
        params={{ libraryId }}
        className="inline-flex items-center gap-1.5 text-sm text-ink-muted transition-colors hover:text-ink"
      >
        <ArrowLeft size={15} /> Back to library
      </Link>

      <div className="mt-4">
        <Eyebrow>Document</Eyebrow>
        <h1 className="mt-2 font-display text-3xl tracking-tight text-ink">{tree?.filename ?? "…"}</h1>
      </div>

      {q.isLoading ? <p className="mt-8 text-sm text-ink-muted">Loading…</p> : null}
      {q.isError ? (
        <Card className="mt-8 border-red-500/30 p-5 text-sm text-red-400">{(q.error as Error).message}</Card>
      ) : null}
      {tree ? (
        <article className="mt-4 max-w-3xl">
          {(tree.children ?? []).map((child, i) => (
            <NodeView key={`root-${i}`} node={child} />
          ))}
        </article>
      ) : null}
    </div>
  );
}

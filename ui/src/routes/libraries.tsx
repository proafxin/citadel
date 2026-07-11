import { CreateLibraryDialog } from "@/components/create-library-dialog";
import { Eyebrow } from "@/components/eyebrow";
import { TierBadge } from "@/components/tier-badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { deleteLibrary, listLibraries } from "@/lib/api";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Plus, Trash2 } from "lucide-react";
import { motion } from "motion/react";
import type { ReactNode } from "react";

export function LibrariesPage() {
  const qc = useQueryClient();
  const { data, isLoading, isError, error } = useQuery({ queryKey: ["libraries"], queryFn: listLibraries });
  const del = useMutation({
    mutationFn: deleteLibrary,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["libraries"] }),
  });

  return (
    <div>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <Eyebrow>Your workspace</Eyebrow>
          <h1 className="mt-2 font-display text-4xl tracking-tight text-ink">Libraries</h1>
        </div>
        <CreateLibraryDialog>
          <Button>
            <Plus size={16} /> New library
          </Button>
        </CreateLibraryDialog>
      </div>

      <div className="mt-8">
        {isLoading ? <GridSkeleton /> : null}
        {isError ? <Notice>{(error as Error).message}</Notice> : null}
        {data && data.length === 0 ? <EmptyState /> : null}
        {data && data.length > 0 ? (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {data.map((lib, i) => (
              <motion.div
                key={lib.id}
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.22, delay: i * 0.03, ease: [0.16, 1, 0.3, 1] }}
              >
                <Card className="group relative transition-colors hover:border-ink-muted/40">
                  <Link to="/library/$libraryId" params={{ libraryId: String(lib.id) }} className="block p-5">
                    <TierBadge tier={lib.tier} />
                    <h2 className="mt-3 font-display text-xl text-ink">{lib.name}</h2>
                  </Link>
                  <button
                    type="button"
                    aria-label="Delete library"
                    onClick={() => {
                      if (window.confirm(`Delete "${lib.name}" and all its documents?`)) del.mutate(lib.id);
                    }}
                    className="absolute right-3 top-3 grid size-8 place-items-center rounded-lg text-ink-muted opacity-0 transition-all hover:bg-surface-2 hover:text-red-400 focus-visible:opacity-100 group-hover:opacity-100"
                  >
                    <Trash2 size={15} />
                  </button>
                </Card>
              </motion.div>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function GridSkeleton() {
  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
      {[0, 1, 2].map((i) => (
        <div key={i} className="h-28 animate-pulse rounded-xl border border-border bg-surface" />
      ))}
    </div>
  );
}

function Notice({ children }: { children: ReactNode }) {
  return <Card className="border-red-500/30 p-5 text-sm text-red-400">Couldn’t load libraries — {children}</Card>;
}

function EmptyState() {
  return (
    <Card className="flex flex-col items-center justify-center gap-4 border-dashed p-14 text-center">
      <p className="text-sm text-ink-muted">No libraries yet.</p>
      <CreateLibraryDialog>
        <Button>
          <Plus size={16} /> New library
        </Button>
      </CreateLibraryDialog>
    </Card>
  );
}

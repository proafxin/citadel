import * as Dialog from "@radix-ui/react-dialog";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import { motion } from "motion/react";
import { type ReactNode, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { createLibrary, type Tier } from "@/lib/api";
import { cn } from "@/lib/cn";
import { TIERS } from "@/lib/tiers";

export function CreateLibraryDialog({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [tier, setTier] = useState<Tier>("tier_1");

  const mutation = useMutation({
    mutationFn: () => createLibrary(name.trim(), tier),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["libraries"] });
      setOpen(false);
      setName("");
      setTier("tier_1");
    },
  });

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>{children}</Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-[#02070d]/60 backdrop-blur-sm" />
        <Dialog.Content asChild>
          <motion.div
            initial={{ opacity: 0, scale: 0.97, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            transition={{ duration: 0.16, ease: [0.16, 1, 0.3, 1] }}
            className="fixed left-1/2 top-1/2 z-50 w-[min(30rem,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-border bg-surface p-6 shadow-2xl"
          >
            <div className="flex items-start justify-between">
              <div>
                <div className="eyebrow">New library</div>
                <Dialog.Title className="mt-1 font-display text-2xl text-ink">Create a library</Dialog.Title>
              </div>
              <Dialog.Close className="grid size-8 place-items-center rounded-lg text-ink-muted transition-colors hover:bg-surface-2 hover:text-ink">
                <X size={16} />
              </Dialog.Close>
            </div>

            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (name.trim()) mutation.mutate();
              }}
              className="mt-5 space-y-5"
            >
              <div className="space-y-1.5">
                <label htmlFor="lib-name" className="eyebrow">
                  Name
                </label>
                <Input
                  id="lib-name"
                  autoFocus
                  placeholder="e.g. Contracts 2026"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </div>

              <div className="space-y-1.5">
                <span className="eyebrow">Purpose</span>
                <div className="grid grid-cols-2 gap-2">
                  {TIERS.map((t) => (
                    <button
                      key={t.value}
                      type="button"
                      onClick={() => setTier(t.value)}
                      className={cn(
                        "rounded-xl border p-3 text-left transition-colors",
                        tier === t.value ? "border-accent bg-surface-2" : "border-border hover:border-ink-muted",
                      )}
                    >
                      <div className="eyebrow">{t.badge}</div>
                      <div className="mt-1 font-display text-lg text-ink">{t.label}</div>
                      <p className="mt-1 text-xs leading-snug text-ink-muted">{t.description}</p>
                    </button>
                  ))}
                </div>
              </div>

              {mutation.isError ? <p className="text-sm text-red-400">{(mutation.error as Error).message}</p> : null}

              <div className="flex justify-end gap-2">
                <Dialog.Close asChild>
                  <Button type="button" variant="ghost">
                    Cancel
                  </Button>
                </Dialog.Close>
                <Button type="submit" disabled={!name.trim() || mutation.isPending}>
                  {mutation.isPending ? "Creating…" : "Create library"}
                </Button>
              </div>
            </form>
          </motion.div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

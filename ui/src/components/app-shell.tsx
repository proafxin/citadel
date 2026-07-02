import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { ThemeToggle } from "@/components/theme-toggle";

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-40 border-b border-border bg-canvas/80 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-5">
          <Link to="/" className="flex items-center gap-2.5">
            <span className="grid size-6 place-items-center rounded-[5px] bg-accent font-display text-sm font-bold text-accent-ink">
              C
            </span>
            <span className="text-sm font-semibold uppercase tracking-[0.28em] text-ink">Citadel</span>
          </Link>
          <ThemeToggle />
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-5 py-10 sm:py-12">{children}</main>
    </div>
  );
}

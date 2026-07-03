import { Monitor, Moon, Sun } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { cn } from "@/lib/cn";
import { useTheme } from "@/lib/theme";

export function ThemeToggle() {
  const { mode, theme, toggle, setSystem } = useTheme();
  const ThemeIcon = theme === "dark" ? Moon : Sun;

  return (
    <div className="flex items-center gap-0.5">
      <button
        type="button"
        onClick={toggle}
        aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
        className="relative grid size-9 place-items-center rounded-lg text-ink-muted transition-colors hover:bg-surface-2 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]"
      >
        <AnimatePresence mode="wait" initial={false}>
          <motion.span
            key={theme}
            initial={{ opacity: 0, y: -6, rotate: -30 }}
            animate={{ opacity: 1, y: 0, rotate: 0 }}
            exit={{ opacity: 0, y: 6, rotate: 30 }}
            transition={{ duration: 0.16, ease: [0.16, 1, 0.3, 1] }}
          >
            <ThemeIcon size={17} />
          </motion.span>
        </AnimatePresence>
      </button>
      <button
        type="button"
        onClick={setSystem}
        aria-pressed={mode === "system"}
        aria-label="Use system theme"
        className={cn(
          "grid size-9 place-items-center rounded-lg transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]",
          mode === "system" ? "bg-surface-2 text-accent" : "text-ink-muted hover:bg-surface-2 hover:text-ink",
        )}
      >
        <Monitor size={17} />
      </button>
    </div>
  );
}

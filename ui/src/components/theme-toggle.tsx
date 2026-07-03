import { Monitor, Moon, Sun } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useTheme } from "@/lib/theme";

const ICON = { light: Sun, dark: Moon, system: Monitor };
const NEXT = { light: "dark", dark: "auto", system: "light" };

export function ThemeToggle() {
  const { mode, cycle } = useTheme();
  const Icon = ICON[mode];

  return (
    <button
      type="button"
      onClick={cycle}
      aria-label={`Theme: ${mode === "system" ? "auto" : mode}. Switch to ${NEXT[mode]}.`}
      className="relative grid size-9 place-items-center rounded-lg text-ink-muted transition-colors hover:bg-surface-2 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]"
    >
      <AnimatePresence mode="wait" initial={false}>
        <motion.span
          key={mode}
          initial={{ opacity: 0, y: -6, rotate: -30 }}
          animate={{ opacity: 1, y: 0, rotate: 0 }}
          exit={{ opacity: 0, y: 6, rotate: 30 }}
          transition={{ duration: 0.16, ease: [0.16, 1, 0.3, 1] }}
        >
          <Icon size={17} />
        </motion.span>
      </AnimatePresence>
    </button>
  );
}

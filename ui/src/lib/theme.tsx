import { type ReactNode, createContext, use, useCallback, useEffect, useState } from "react";

type Mode = "light" | "dark" | "system";
type Theme = "light" | "dark";

type ThemeContextValue = {
  mode: Mode;
  theme: Theme;
  toggle: () => void;
  setSystem: () => void;
};

const ThemeContext = createContext<ThemeContextValue | null>(null);
const STORAGE_KEY = "citadel-theme-mode";

function storedMode(): Mode {
  const value = localStorage.getItem(STORAGE_KEY);
  return value === "light" || value === "dark" || value === "system" ? value : "system";
}

function prefersDark(): boolean {
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<Mode>(storedMode);
  const [systemDark, setSystemDark] = useState<boolean>(prefersDark);

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => setSystemDark(media.matches);
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);

  const theme: Theme = mode === "system" ? (systemDark ? "dark" : "light") : mode;

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);

  const persist = useCallback((next: Mode) => {
    localStorage.setItem(STORAGE_KEY, next);
    setMode(next);
  }, []);
  const toggle = useCallback(() => persist(theme === "dark" ? "light" : "dark"), [persist, theme]);
  const setSystem = useCallback(() => persist("system"), [persist]);

  return <ThemeContext value={{ mode, theme, toggle, setSystem }}>{children}</ThemeContext>;
}

export function useTheme(): ThemeContextValue {
  const ctx = use(ThemeContext);
  if (ctx === null) throw new Error("useTheme must be used within ThemeProvider");
  return ctx;
}

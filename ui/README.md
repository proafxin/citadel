# Citadel UI

Bun + Vite + React 19 + TypeScript + Tailwind v4. Talks to the FastAPI backend.

## Run (dev)

Start the backend first (from the repo root): `bash scripts/run.sh` (FastAPI on `:8000`).

Then:

```bash
cd ui
bun install
bun run dev
```

Open http://localhost:5173. The dev server proxies `/api/*` → `http://localhost:8000`
(configured in `vite.config.ts`), so there's no CORS friction locally.

## Scripts

- `bun run dev` — dev server with HMR
- `bun run build` — typecheck + production build to `dist/`
- `bun run preview` — serve the production build
- `bun run lint` — Biome check
- `bun run format` — Biome format

## Notes

- **Theme:** dark by default (follows OS on first visit), one-click toggle in the header,
  choice persisted to `localStorage`.
- **Fonts:** see `public/fonts/README.md`. Falls back gracefully if absent.
- Not dockerized — `bun run build` emits static files any host (incl. FastAPI) can serve.

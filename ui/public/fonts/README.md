# Fonts

Drop these self-hosted `.woff2` files here to activate the signature typography.
Until they exist, the UI falls back gracefully (system serif / sans / mono), so it
still runs and looks clean without them.

Expected files (referenced by `src/index.css`):

- `NewCMSerif-Regular.woff2` — New Computer Modern serif (display / headings), 400
- `NewCMSerif-Bold.woff2` — New Computer Modern serif, 700
- `NewCMMono-Regular.woff2` — New Computer Modern mono (tabular data / ids)
- `Inter-Variable.woff2` — Inter variable (body / UI)

New Computer Modern is published by GUST under the GUST Font License (free to embed).
Convert the upstream OTFs to `woff2`, or grab a prebuilt web build, and place them here.

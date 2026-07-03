# Fonts

The whole UI is set in **New Computer Modern** (Book weight — a touch heavier than
Regular, which reads better on screen). Files here are referenced by `src/index.css`.
Until they exist the UI falls back to a serif stack (Latin Modern Roman / Cambria / Georgia).

In use:

- `NewCM10-Book.otf` — serif, 400 (body + display)
- `NewCM10-BookItalic.otf` — serif italic, 400
- `NewCMMono10-Book.otf` — mono (tabular data / ids)

Optional, for crisp (non-synthesized) bold at weight 700:

- `NewCM10-Bold.otf` — the `700` `@font-face` already points at it; without it, bold is faux-bolded from Book.

`License.txt` (GUST Font License) is kept here to satisfy the redistribution condition.
OTF works fine for the web; convert to `woff2` later for smaller/faster assets.

# Web frontend: Vue + mature Markdown

Status: **active** (2026-07-20)

## Research summary

| Approach | Tables / GFM | Streaming | Notes |
| --- | --- | --- | --- |
| Hand-rolled MD (old `index.html`) | **Broken** (no table parser) | full re-render | Caused the pipe-table glitch |
| **markdown-it** + multimd-table | Strong | full re-render | Mature, widely used in chat UIs |
| marked (GFM) | Strong | full re-render | Also fine; we chose markdown-it plugins |
| markstream-vue / Streamdown | Optimized partial | incremental | Heavier; optional later if flicker matters |
| Chrome AI `streaming-markdown` | experimental | token-level | Not needed for personal v1 message sizes |

**Recommendation (implemented):** Vue 3 SPA + **markdown-it** + **markdown-it-multimd-table** + **DOMPurify** + **highlight.js**.  
Streaming strategy: accumulate full Markdown string, re-parse on paint (rAF-friendly). Incomplete tables settle when header/separator/rows complete — industry standard for LLM chat.

## Layout

```
frontend/                  # Vue 3 + Vite + TS source
  src/
    lib/markdown.ts        # render pipeline
    components/…
    App.vue
src/ariadne/web/static/
  dist/                    # `npm run build` output (served by FastAPI)
  index.html               # legacy SPA fallback if dist missing
```

## Dev

```bash
# terminal 1
ariadne serve --port 8420

# terminal 2
cd frontend && npm run dev   # proxies /api → :8420
```

## Build (required before packaging / prod serve)

```bash
cd frontend && npm ci && npm run build:fast
# writes to src/ariadne/web/static/dist/
```

FastAPI serves `static/dist/index.html` when present, else legacy `static/index.html`.

## Workspace browser (left rail)

- Tabs: **历史** (sessions) | **工作区** (`/workspace` tree + preview)
- Root binding follows the single rule (open folder, or atelier main/branch when selected); see
  [web-workspace.md](web-workspace.md). Not per chat session.
- Chat inlines images via `/api/workspace/file` (host absolute paths under
  the active root are rewritten client-side).

## Non-goals

- Separate browser microservice (still one host process)
- Mobile-native clients
- Per-session workspace clones (diverges from Codex project model)

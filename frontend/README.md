# Ariadne Web (Vue 3)

Vue 3 + Vite + TypeScript frontend for the Ariadne agent host.

## Markdown

Uses **markdown-it** + **markdown-it-multimd-table** + **DOMPurify** + **highlight.js**.  
Streaming re-renders the full accumulated Markdown string (standard for chat UIs).

See `../docs/design/web-vue-frontend.md`.

## Commands

```bash
npm install
npm run dev        # http://127.0.0.1:5173  (proxies /api → :8420)
npm run build:fast # → ../src/ariadne/web/static/dist
```

Serve with:

```bash
ariadne serve --port 8420
```

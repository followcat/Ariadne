# README assets

Images used by the root [README.md](../../README.md) and [README.zh-CN.md](../../README.zh-CN.md).

| File | Role |
| --- | --- |
| `hero.jpg` | Banner / product hero |
| `web-demo.jpg` | Web UI — **Vue** dark shell (left history · chat · **right tools panel**) |
| `web-demo-light.jpg` | Web UI — same three-column shell, light theme |
| `cli-demo.jpg` | CLI REPL with tools / diffs / slash commands |

Prefer files under ~250 KB so GitHub renders the intro page quickly.

Screenshots should match the **current** product shell:

- Left: new chat + session list + appearance / Provider / logout  
- Center: top bar (title · theme · tools · session/model chips) · empty/chat · composer  
- Right: collapsible **工具调用** panel  
- Markdown via **markdown-it** (GFM tables, code); optional thinking collapse; turn info (duration / tokens / tools)

Refresh after UI changes:

```bash
# with ariadne serve (or a temporary app) + playwright
# then commit updated docs/assets/web-demo*.jpg
```

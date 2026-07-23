/**
 * Mature Markdown pipeline for chat:
 * - markdown-it (CommonMark + plugins)
 * - multimd-table (GFM-style tables, multi-line cells)
 * - highlight.js for fenced code
 * - DOMPurify sanitize
 *
 * Streaming strategy: re-render the full accumulated string on each paint.
 * This is the industry-standard chat approach (OpenAI forum / Chrome AI docs):
 * incomplete tokens are fixed on the next complete parse; tables settle when
 * header + separator + rows are complete.
 */
import MarkdownIt from 'markdown-it'
import multimdTable from 'markdown-it-multimd-table'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/core'
import javascript from 'highlight.js/lib/languages/javascript'
import typescript from 'highlight.js/lib/languages/typescript'
import python from 'highlight.js/lib/languages/python'
import bash from 'highlight.js/lib/languages/bash'
import json from 'highlight.js/lib/languages/json'
import xml from 'highlight.js/lib/languages/xml'
import sql from 'highlight.js/lib/languages/sql'
import yaml from 'highlight.js/lib/languages/yaml'
import markdown from 'highlight.js/lib/languages/markdown'

hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('js', javascript)
hljs.registerLanguage('typescript', typescript)
hljs.registerLanguage('ts', typescript)
hljs.registerLanguage('python', python)
hljs.registerLanguage('py', python)
hljs.registerLanguage('bash', bash)
hljs.registerLanguage('sh', bash)
hljs.registerLanguage('shell', bash)
hljs.registerLanguage('json', json)
hljs.registerLanguage('xml', xml)
hljs.registerLanguage('html', xml)
hljs.registerLanguage('sql', sql)
hljs.registerLanguage('yaml', yaml)
hljs.registerLanguage('yml', yaml)
hljs.registerLanguage('markdown', markdown)
hljs.registerLanguage('md', markdown)

/** Image extensions we auto-inline from the sandbox workspace. */
const IMG_EXT = 'png|jpe?g|gif|webp|svg'
/**
 * Sandbox path segment ending in an image extension.
 * Excludes trailing Chinese punctuation so "path.png。" does not leak into the URL.
 */
const WS_IMG =
  String.raw`\/?workspace\/[^\s)\]"'<>。，、；：！？]+?\.(?:` + IMG_EXT + ')'

function makeMd(withHighlight: boolean) {
  return new MarkdownIt({
    html: false,
    linkify: true,
    breaks: true,
    typographer: false,
    highlight(str, lang) {
      // History loads can contain multi‑KB minified JS; highlight.js freezes the UI.
      if (!withHighlight || str.length > 4000) {
        return '<pre class="hljs"><code>' + escapePlain(str) + '</code></pre>'
      }
      if (lang && hljs.getLanguage(lang)) {
        try {
          return (
            '<pre class="hljs"><code class="language-' +
            escapePlain(lang) +
            '">' +
            hljs.highlight(str, { language: lang, ignoreIllegals: true }).value +
            '</code></pre>'
          )
        } catch {
          /* fall through */
        }
      }
      return '<pre class="hljs"><code>' + escapePlain(str) + '</code></pre>'
    },
  })
}

function escapePlain(s: string): string {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/** Full pipeline for streaming answers (syntax highlight, capped). */
const md = makeMd(true)
/** Fast pipeline for history load — no highlight.js work. */
const mdLite = makeMd(false)

// GFM-like tables (pipes, alignment row, multi-line cells)
for (const engine of [md, mdLite]) {
  engine.use(multimdTable, {
    multiline: true,
    rowspan: false,
    headerless: false,
    multibody: true,
  })
}

function patchLinkOpen(engine: MarkdownIt) {
  const defaultLinkOpen =
    engine.renderer.rules.link_open ||
    ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))
  engine.renderer.rules.link_open = (tokens, idx, options, env, self) => {
    const token = tokens[idx]
    const aIndex = token.attrIndex('target')
    if (aIndex < 0) token.attrPush(['target', '_blank'])
    else token.attrs![aIndex][1] = '_blank'
    const rIndex = token.attrIndex('rel')
    if (rIndex < 0) token.attrPush(['rel', 'noopener noreferrer'])
    else token.attrs![rIndex][1] = 'noopener noreferrer'
    return defaultLinkOpen(tokens, idx, options, env, self)
  }
}
patchLinkOpen(md)
patchLinkOpen(mdLite)


/**
 * Optional host absolute workspace root from /api/me (e.g. /home/…/project).
 * When set, host paths under this root are rewritten to /workspace/… virtual paths.
 */
let hostWorkspaceRoot = ''
/**
 * Active atelier id for image/file API scoping. Without this, /api/workspace/file
 * hits the default project workspace and atelier outputs (e.g. moonlight_bird.png)
 * 404 → broken chat images.
 */
let activeAtelierId = ''
let activeAtelierSession = ''

export function setHostWorkspaceRoot(root: string | null | undefined) {
  const r = String(root || '').trim().replace(/\/+$/, '')
  hostWorkspaceRoot = r
}

export function getHostWorkspaceRoot(): string {
  return hostWorkspaceRoot
}

export function setActiveAtelierId(id: string | null | undefined) {
  activeAtelierId = String(id || '').trim()
}

export function setActiveAtelierSession(session: string | null | undefined) {
  activeAtelierSession = String(session || '').trim() || 'main'
}

export function getActiveAtelierId(): string {
  return activeAtelierId
}

/**
 * Attach or refresh atelier_id / atelier_session on workspace file URLs.
 * Must replace existing params so session switches don't keep a stale branch id.
 */
function withAtelierQuery(url: string): string {
  const raw = String(url || '').trim()
  if (!raw.startsWith('/api/workspace/file')) return raw
  try {
    const u = new URL(raw, 'http://ariadne.local')
    if (activeAtelierId) {
      u.searchParams.set('atelier_id', activeAtelierId)
      u.searchParams.set('atelier_session', activeAtelierSession || 'main')
    } else {
      u.searchParams.delete('atelier_id')
      u.searchParams.delete('atelier_session')
    }
    return u.pathname + u.search
  } catch {
    return raw
  }
}

/** Map sandbox / host / relative image paths to the authenticated file API URL. */
export function workspaceFileUrl(rawPath: string): string {
  let p = String(rawPath || '').trim()
  if (!p) return ''
  if (p.startsWith('/api/workspace/file?')) {
    return withAtelierQuery(p)
  }
  // strip optional file:// prefix models sometimes emit
  p = p.replace(/^file:\/\//i, '')
  // Host absolute under known workspace → virtual /workspace/…
  if (hostWorkspaceRoot) {
    const root = hostWorkspaceRoot
    if (p === root) p = '/workspace'
    else if (p.startsWith(root + '/')) {
      p = '/workspace/' + p.slice(root.length + 1)
    }
  }
  if (p.startsWith('workspace/')) p = '/' + p
  if (!p.startsWith('/')) p = '/workspace/' + p
  // Accept /workspace/… or remaining host absolute (API confines to workspace)
  if (!/^\/workspace\//i.test(p) && p !== '/workspace' && !p.startsWith('/')) {
    return ''
  }
  // Host absolute paths (no virtual prefix) are allowed; server rejects escapes.
  if (!/^\/workspace(\/|$)/i.test(p) && !p.startsWith('/')) return ''
  return withAtelierQuery('/api/workspace/file?path=' + encodeURIComponent(p))
}

/**
 * Normalize img src that points at the sandbox into /api/workspace/file?…
 * Safe to call multiple times (idempotent for already-rewritten API URLs).
 */
export function rewriteWorkspaceSrc(src: string): string {
  const s = String(src || '').trim()
  if (!s) return s
  // Always refresh atelier query — history/cache may carry a stale session.
  if (s.startsWith('/api/workspace/file?')) return withAtelierQuery(s)
  if (/^(?:https?:|data:|blob:)/i.test(s)) return s
  const stripped = s.replace(/^file:\/\//i, '')
  if (/^\/?workspace\//i.test(stripped)) {
    const p = stripped.startsWith('/') ? stripped : '/' + stripped
    return workspaceFileUrl(p) || s
  }
  // Host absolute under known workspace root
  if (hostWorkspaceRoot && stripped.startsWith(hostWorkspaceRoot)) {
    return workspaceFileUrl(stripped) || s
  }
  // Any absolute path ending in an image ext — let the API gate confinement
  if (
    stripped.startsWith('/') &&
    !stripped.startsWith('/api/') &&
    new RegExp(String.raw`\.(?:` + IMG_EXT + ')$', 'i').test(stripped)
  ) {
    return workspaceFileUrl(stripped) || s
  }
  // Relative workspace image: plot.png or ./charts/a.png
  if (/^(?:\.\/)?[\w.-]+(?:\/[\w.-]+)*\.(?:png|jpe?g|gif|webp|svg)$/i.test(stripped)) {
    return workspaceFileUrl('/workspace/' + stripped.replace(/^\.\//, '')) || s
  }
  return s
}

// At render time: convert ![alt](/workspace/…) → authenticated API URL so
// DOMPurify (which only allows /api/workspace/file?) keeps the src.
function patchImage(engine: MarkdownIt) {
  const defaultImage =
    engine.renderer.rules.image ||
    ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))
  engine.renderer.rules.image = (tokens, idx, options, env, self) => {
    const token = tokens[idx]
    const srcIdx = token.attrIndex('src')
    if (srcIdx >= 0 && token.attrs) {
      token.attrs[srcIdx][1] = rewriteWorkspaceSrc(token.attrs[srcIdx][1] || '')
    }
    return defaultImage(tokens, idx, options, env, self)
  }
}
patchImage(md)
patchImage(mdLite)

const PURIFY: DOMPurify.Config = {
  USE_PROFILES: { html: true },
  ADD_ATTR: ['target', 'rel', 'class', 'src', 'alt', 'data-workspace-src'],
  ALLOWED_URI_REGEXP:
    /^(?:(?:https?|mailto):|data:image\/|\/api\/workspace\/file\?|#)/i,
}

/** Apply `fn` only outside fenced code blocks (``` / ~~~). */
function mapOutsideFences(src: string, fn: (chunk: string) => string): string {
  return String(src || '')
    .split(/(```[\s\S]*?```|~~~[\s\S]*?~~~)/)
    .map((chunk) => {
      if (chunk.startsWith('```') || chunk.startsWith('~~~')) return chunk
      return fn(chunk)
    })
    .join('')
}

/**
 * Turn bare /workspace/*.png (and markdown images) into API-backed image markdown.
 * Models often emit either:
 *   - bare path:  已保存 /workspace/plot.png
 *   - md image:   ![走势图](/workspace/plot.png)  ← must rewrite or DOMPurify strips src
 *   - backticks:  `/workspace/plot.png`
 *   - file URI:   file:///workspace/plot.png
 *
 * Never re-write the "workspace/file" segment of /api/workspace/file?…
 */
export function rewriteWorkspaceImages(src: string): string {
  return mapOutsideFences(src, (chunk) => {
    let s = chunk

    // 0) Normalize file:///workspace/… → /workspace/… (md + bare + backticks)
    s = s.replace(/file:\/\/(\/workspace\/)/gi, '$1')

    // 1) Markdown images using sandbox paths → API URL (keep alt text)
    s = s.replace(
      new RegExp(String.raw`!\[([^\]]*)\]\((${WS_IMG})\)`, 'gi'),
      (_m, alt: string, path: string) => {
        const url = workspaceFileUrl(path)
        if (!url) return _m
        return `![${alt || '图片'}](${url})`
      },
    )

    // 1b) Relative / plain filenames in markdown images → /workspace/…
    //     e.g. ![走势图](a_share_5days_trend.png)  or  ![x](./out/plot.png)
    s = s.replace(
      new RegExp(
        String.raw`!\[([^\]]*)\]\((?!https?:|data:|blob:|\/api\/)(\.?\/?(?:[\w.-]+\/)*[\w.-]+\.(?:` +
          IMG_EXT +
          '))\\)',
        'gi',
      ),
      (_m, alt: string, path: string) => {
        let p = String(path || '').replace(/^\.\//, '')
        if (p.startsWith('/workspace/')) {
          /* already handled above */
        } else if (p.startsWith('workspace/')) {
          p = '/' + p
        } else if (p.startsWith('/')) {
          // host absolute (e.g. /home/…/plot.png) — API confines to workspace
        } else {
          p = '/workspace/' + p
        }
        const url = workspaceFileUrl(p)
        if (!url) return _m
        return `![${alt || '图片'}](${url})`
      },
    )

    // 2) Single-path backticks → image (sandbox or host absolute)
    s = s.replace(
      new RegExp(
        '`' +
          String.raw`((?:\/?workspace\/|\/(?!api\/)[^\s` +
          '`' +
          String.raw`]*)[^\s` +
          '`' +
          String.raw`]+\.(?:` +
          IMG_EXT +
          '))' +
          '`',
        'gi',
      ),
      (_m, path: string) => {
        const url = workspaceFileUrl(path)
        if (!url) return _m
        return `\n\n![图片](${url})\n\n`
      },
    )

    // 3) Bare "/workspace/foo.png" — require a non-identifier char before the slash
    //    so "/api/workspace/file?…" is not matched (char before /workspace is "i").
    s = s.replace(
      new RegExp(
        String.raw`(^|[^a-zA-Z0-9_])(\/workspace\/[^\s)\]"'<>。，、；：！？]+?\.(?:` +
          IMG_EXT +
          '))',
        'gi',
      ),
      (full, prefix: string, path: string) => {
        // still inside a markdown destination: ![alt](…)
        if (prefix.endsWith('](') || prefix === '(' || prefix.endsWith('(')) {
          return full
        }
        const url = workspaceFileUrl(path)
        if (!url) return full
        return `${prefix}\n\n![走势图](${url})\n\n`
      },
    )

    // 4) Relative "workspace/foo.png" without leading slash
    s = s.replace(
      new RegExp(
        String.raw`(^|[^a-zA-Z0-9_/])(workspace\/[^\s)\]"'<>。，、；：！？]+?\.(?:` +
          IMG_EXT +
          '))',
        'gi',
      ),
      (full, prefix: string, path: string) => {
        // "/api/" + "workspace/…" → prefix is "/" — skip
        if (prefix === '/' || prefix.endsWith('](') || prefix.endsWith('(')) {
          return full
        }
        const url = workspaceFileUrl(path)
        if (!url) return full
        return `${prefix}\n\n![走势图](${url})\n\n`
      },
    )

    // 5) Host absolute image paths models print as real FS paths
    //    e.g. /home/followcat/Projects/Ariadne/a_share_5days_trend.png
    //    Skip /workspace/… (step 3) and /api/… (false positive).
    s = s.replace(
      new RegExp(
        String.raw`(^|[^a-zA-Z0-9_])(\/(?!api\/|workspace\/)[^\s)\]"'<>。，、；：！？]+\.(?:` +
          IMG_EXT +
          '))',
        'gi',
      ),
      (full, prefix: string, path: string) => {
        if (prefix.endsWith('](') || prefix.endsWith('(')) return full
        const url = workspaceFileUrl(path)
        if (!url) return full
        return `${prefix}\n\n![走势图](${url})\n\n`
      },
    )

    return s
  })
}

/** Normalize model quirks so tables parse more reliably. */
export function normalizeMarkdown(src: string): string {
  let s = String(src || '').replace(/\r\n?/g, '\n')
  // Prefer 走势图 wording over generic 图表 when models describe plots
  s = s.replace(/完整图表/g, '完整走势图')
  s = s.replace(/图表内容/g, '走势图内容')
  s = s.replace(/查看完整图表/g, '查看完整走势图')
  s = s.replace(/图表已保存/g, '走势图已保存')
  // Some models emit a single-line "table" with spaces around pipes — ensure
  // blank line before a table block so markdown-it starts a table context.
  s = s.replace(/([^\n])\n(\|[^\n]+\|\s*\n\|[-:| ]+\|)/g, '$1\n\n$2')
  // Fix separator rows that use only --- without leading pipes
  s = s.replace(/^(\s*)\|?(\s*:?-{3,}:?\s*\|)+\s*$/gm, (line) => {
    if (line.trim().startsWith('|')) return line
    return '| ' + line.trim().split('|').filter(Boolean).join(' | ') + ' |'
  })
  s = rewriteWorkspaceImages(s)
  return s
}

export type RenderMarkdownOpts = {
  /** Use syntax highlighting (streaming answers). History should pass false. */
  highlight?: boolean
  /** Soft-cap source length for fast history paint (default unlimited). */
  maxSourceChars?: number
}

/** Rendered HTML cache — switching sessions back is instant. */
const htmlCache = new Map<string, string>()
const HTML_CACHE_MAX = 200

export function renderMarkdown(src: string, opts: RenderMarkdownOpts = {}): string {
  const highlight = opts.highlight !== false
  let text = String(src || '')
  const cap = opts.maxSourceChars
  let truncated = false
  if (cap && text.length > cap) {
    text = text.slice(0, cap)
    truncated = true
  }
  // Scope cache by atelier session — same markdown, different file roots.
  const scope = activeAtelierId
    ? `a:${activeAtelierId}:${activeAtelierSession || 'main'}:`
    : 's:'
  const cacheKey =
    scope + (highlight ? 'h:' : 'l:') + (truncated ? 't:' : '') + text
  const hit = htmlCache.get(cacheKey)
  if (hit) return hit

  const normalized = normalizeMarkdown(text)
  const engine = highlight ? md : mdLite
  let dirty = engine.render(normalized)
  if (truncated) {
    dirty +=
      '<p class="md-truncated-note"><em>…内容较长，已折叠显示前半部分</em></p>'
  }
  const html = DOMPurify.sanitize(dirty, PURIFY)
  if (htmlCache.size >= HTML_CACHE_MAX) {
    const first = htmlCache.keys().next().value
    if (first !== undefined) htmlCache.delete(first)
  }
  htmlCache.set(cacheKey, html)
  return html
}

export function escapeHtml(s: string): string {
  return escapePlain(s)
}

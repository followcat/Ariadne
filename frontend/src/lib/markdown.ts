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

const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
  typographer: false,
  highlight(str, lang) {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return (
          '<pre class="hljs"><code class="language-' +
          md.utils.escapeHtml(lang) +
          '">' +
          hljs.highlight(str, { language: lang, ignoreIllegals: true }).value +
          '</code></pre>'
        )
      } catch {
        /* fall through */
      }
    }
    return (
      '<pre class="hljs"><code>' + md.utils.escapeHtml(str) + '</code></pre>'
    )
  },
})

// GFM-like tables (pipes, alignment row, multi-line cells)
md.use(multimdTable, {
  multiline: true,
  rowspan: false,
  headerless: false,
  multibody: true,
})

// Open links in new tab
const defaultLinkOpen =
  md.renderer.rules.link_open ||
  ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))

md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  const token = tokens[idx]
  const aIndex = token.attrIndex('target')
  if (aIndex < 0) token.attrPush(['target', '_blank'])
  else token.attrs![aIndex][1] = '_blank'
  const rIndex = token.attrIndex('rel')
  if (rIndex < 0) token.attrPush(['rel', 'noopener noreferrer'])
  else token.attrs![rIndex][1] = 'noopener noreferrer'
  return defaultLinkOpen(tokens, idx, options, env, self)
}

/** Map a sandbox path to the authenticated file API URL. */
export function workspaceFileUrl(rawPath: string): string {
  let p = String(rawPath || '').trim()
  if (!p) return ''
  // strip optional file:// prefix models sometimes emit
  p = p.replace(/^file:\/\//i, '')
  if (p.startsWith('workspace/')) p = '/' + p
  if (!p.startsWith('/')) p = '/' + p
  if (!/^\/workspace\//i.test(p)) return ''
  return '/api/workspace/file?path=' + encodeURIComponent(p)
}

/**
 * Normalize img src that points at the sandbox into /api/workspace/file?…
 * Safe to call multiple times (idempotent for already-rewritten API URLs).
 */
export function rewriteWorkspaceSrc(src: string): string {
  const s = String(src || '').trim()
  if (!s) return s
  if (s.startsWith('/api/workspace/file?')) return s
  if (/^(?:https?:|data:|blob:)/i.test(s)) return s
  const stripped = s.replace(/^file:\/\//i, '')
  if (/^\/?workspace\//i.test(stripped)) {
    const p = stripped.startsWith('/') ? stripped : '/' + stripped
    return workspaceFileUrl(p) || s
  }
  // Relative workspace image: plot.png or ./charts/a.png
  if (/^(?:\.\/)?[\w.-]+(?:\/[\w.-]+)*\.(?:png|jpe?g|gif|webp|svg)$/i.test(stripped)) {
    return workspaceFileUrl('/workspace/' + stripped.replace(/^\.\//, '')) || s
  }
  return s
}

// At render time: convert ![alt](/workspace/…) → authenticated API URL so
// DOMPurify (which only allows /api/workspace/file?) keeps the src.
const defaultImage =
  md.renderer.rules.image ||
  ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))

md.renderer.rules.image = (tokens, idx, options, env, self) => {
  const token = tokens[idx]
  const srcIdx = token.attrIndex('src')
  if (srcIdx >= 0 && token.attrs) {
    token.attrs[srcIdx][1] = rewriteWorkspaceSrc(token.attrs[srcIdx][1] || '')
  }
  return defaultImage(tokens, idx, options, env, self)
}

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
        return `![${alt || '走势图'}](${url})`
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
          // absolute non-workspace path — do not force
          return _m
        } else {
          p = '/workspace/' + p
        }
        const url = workspaceFileUrl(p)
        if (!url) return _m
        return `![${alt || '走势图'}](${url})`
      },
    )

    // 2) Single-path backticks → image (before bare, so we don't leave stray `)
    s = s.replace(
      new RegExp(
        '`' +
          String.raw`(\/?workspace\/[^` +
          '`' +
          String.raw`\s]+\.(?:` +
          IMG_EXT +
          '))' +
          '`',
        'gi',
      ),
      (_m, path: string) => {
        const url = workspaceFileUrl(path)
        if (!url) return _m
        return `\n\n![走势图](${url})\n\n`
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

export function renderMarkdown(src: string): string {
  const normalized = normalizeMarkdown(src)
  const dirty = md.render(normalized)
  return DOMPurify.sanitize(dirty, PURIFY)
}

export function escapeHtml(s: string): string {
  return md.utils.escapeHtml(String(s ?? ''))
}

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

const PURIFY: DOMPurify.Config = {
  USE_PROFILES: { html: true },
  ADD_ATTR: ['target', 'rel', 'class', 'src', 'alt', 'data-workspace-src'],
  ALLOWED_URI_REGEXP:
    /^(?:(?:https?|mailto):|data:image\/|\/api\/workspace\/file\?|#)/i,
}

/** Turn bare /workspace/*.png paths into markdown images (走势图). */
export function rewriteWorkspaceImages(src: string): string {
  // Avoid matching already-markdown images: ![alt](/workspace/...)
  return String(src || '').replace(
    /(^|[^\](])(\/?workspace\/[^\s)\]"'<>]+\.(?:png|jpe?g|gif|webp|svg))/gi,
    (_full, prefix: string, path: string) => {
      if (String(prefix).endsWith('](')) return _full
      const p = path.startsWith('/') ? path : '/' + path
      const api = '/api/workspace/file?path=' + encodeURIComponent(p)
      return `${prefix}\n\n![走势图](${api})\n\n`
    },
  )
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

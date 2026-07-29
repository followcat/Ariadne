/**
 * Structural check: fenced mermaid/svg become in-chat diagram containers.
 * Run from frontend/: node scripts/check-markdown-diagrams.mjs
 */
import { JSDOM } from 'jsdom'
import { pathToFileURL } from 'node:url'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')

const dom = new JSDOM('<!DOCTYPE html><html><body></body></html>', {
  url: 'http://localhost/',
})
globalThis.window = dom.window
globalThis.document = dom.window.document
globalThis.DOMParser = dom.window.DOMParser
globalThis.Node = dom.window.Node
globalThis.HTMLElement = dom.window.HTMLElement
globalThis.Element = dom.window.Element
globalThis.DocumentFragment = dom.window.DocumentFragment

const { renderMarkdown } = await import(
  pathToFileURL(path.join(root, 'src/lib/markdown.ts')).href
)

const mermaidHtml = renderMarkdown(
  '## chart\n\n```mermaid\ngraph TB\n  A-->B\n```\n',
  { highlight: true },
)
if (!mermaidHtml.includes('md-mermaid')) {
  console.error('FAIL: missing md-mermaid wrapper')
  process.exit(1)
}
if (!mermaidHtml.includes('class="mermaid"') && !mermaidHtml.includes("class='mermaid'")) {
  console.error('FAIL: missing pre.mermaid')
  process.exit(1)
}
if (!mermaidHtml.includes('graph TB')) {
  console.error('FAIL: mermaid source stripped')
  process.exit(1)
}
// Must NOT be double-wrapped in <pre><code class="language-mermaid">…
if (/<pre><code[^>]*language-mermaid/i.test(mermaidHtml)) {
  console.error('FAIL: mermaid still double-wrapped in pre>code\n', mermaidHtml.slice(0, 300))
  process.exit(1)
}

const svgSrc =
  '```svg\n<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><circle cx="5" cy="5" r="4" fill="red"/></svg>\n```\n'
const svgHtml = renderMarkdown(svgSrc, { highlight: true })
const svgHtmlLite = renderMarkdown(svgSrc, { highlight: false })
if (!svgHtml.includes('md-svg')) {
  console.error('FAIL: missing md-svg wrapper')
  process.exit(1)
}
if (!/circle/i.test(svgHtml)) {
  console.error('FAIL: svg content stripped by sanitize')
  process.exit(1)
}
// History path uses lite (highlight:false) — attrs must survive DOMPurify
if (!/cx="5"/i.test(svgHtmlLite) || !/fill="red"/i.test(svgHtmlLite)) {
  console.error(
    'FAIL: SVG presentation attrs stripped on history/lite path\n',
    svgHtmlLite.slice(0, 400),
  )
  process.exit(1)
}
if (/<pre><code[^>]*language-svg/i.test(svgHtmlLite)) {
  console.error('FAIL: svg still double-wrapped in pre>code\n', svgHtmlLite.slice(0, 300))
  process.exit(1)
}

// File download links (non-image workspace paths)
const fileHtml = renderMarkdown(
  '见报告 `/workspace/out/report.md` 与 [说明](/workspace/docs/note.txt)\n',
  { highlight: true },
)
if (!fileHtml.includes('md-ws-file') && !fileHtml.includes('/api/workspace/file')) {
  console.error('FAIL: workspace file not linked for download\n', fileHtml.slice(0, 400))
  process.exit(1)
}
if (!/report\.md|note\.txt/.test(fileHtml)) {
  console.error('FAIL: file name missing from link text')
  process.exit(1)
}

console.log('check-markdown-diagrams: OK')

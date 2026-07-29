/**
 * Lazy Mermaid render for chat markdown (```mermaid / ```mmd fences).
 * Called after v-html paint; incomplete streaming graphs fail softly.
 *
 * Uses mermaid.render() per block (not mermaid.run) so history reload with
 * many diagrams gets stable unique IDs and explicit SVG injection.
 */

let initialized = false
let initTheme = ''
let renderSeq = 0

function currentTheme(): 'default' | 'dark' {
  const t = document.documentElement.getAttribute('data-theme') || 'dark'
  return t === 'light' ? 'default' : 'dark'
}

async function ensureMermaid() {
  const mermaid = (await import('mermaid')).default
  const theme = currentTheme()
  if (!initialized || initTheme !== theme) {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      theme,
      fontFamily: 'ui-sans-serif, system-ui, sans-serif',
      flowchart: { htmlLabels: true, curve: 'basis' },
    })
    initialized = true
    initTheme = theme
  }
  return mermaid
}

function pendingMermaidNodes(root: HTMLElement): HTMLElement[] {
  return [
    ...root.querySelectorAll<HTMLElement>('.md-mermaid pre.mermaid'),
  ].filter((el) => {
    if (el.getAttribute('data-processed') === 'true') return false
    // Already has injected SVG (sibling or child)
    const wrap = el.closest('.md-mermaid')
    if (wrap?.querySelector('svg')) return false
    if (el.querySelector('svg')) return false
    const text = (el.textContent || '').trim()
    if (!text || text.length < 8) return false
    return true
  })
}

/**
 * Find pending mermaid blocks under root and render them in place.
 * Safe to call repeatedly (skips already-rendered SVG containers).
 */
export async function renderMermaidIn(root: HTMLElement | null): Promise<void> {
  if (!root) return
  const nodes = pendingMermaidNodes(root)
  if (!nodes.length) return

  let mermaid: Awaited<ReturnType<typeof ensureMermaid>>
  try {
    mermaid = await ensureMermaid()
  } catch {
    return
  }

  for (const el of nodes) {
    // Node may have been detached while we awaited
    if (!el.isConnected) continue
    if (el.getAttribute('data-processed') === 'true') continue
    const text = (el.textContent || '').trim()
    if (!text) continue

    const id = `ariadne-mmd-${Date.now().toString(36)}-${++renderSeq}`
    try {
      const { svg } = await mermaid.render(id, text)
      if (!el.isConnected) continue
      el.setAttribute('data-processed', 'true')
      el.setAttribute('hidden', '')
      const wrap = el.closest('.md-mermaid') as HTMLElement | null
      const host = wrap || el.parentElement || el
      // Remove previous failed/partial SVG if any
      host.querySelectorAll(':scope > svg').forEach((s) => s.remove())
      const holder = document.createElement('div')
      holder.innerHTML = svg
      const svgEl = holder.querySelector('svg')
      if (svgEl) {
        host.appendChild(svgEl)
      } else {
        // Unexpected shape — fall back to injecting raw
        host.insertAdjacentHTML('beforeend', svg)
      }
      if (wrap) wrap.classList.add('md-mermaid-done')
    } catch {
      // Incomplete diagram while streaming — leave source pre visible
      el.removeAttribute('data-processed')
    }
  }
}

/** True when markdown source contains diagram fences or .svg media. */
export function contentHasDiagrams(source: string): boolean {
  const s = String(source || '')
  if (/```\s*(?:mermaid|mmd|svg)\b/i.test(s)) return true
  if (/~~~+\s*(?:mermaid|mmd|svg)\b/i.test(s)) return true
  if (/\.svg(\?|#|$|\)|\s)/i.test(s)) return true
  return false
}

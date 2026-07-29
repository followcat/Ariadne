<script setup lang="ts">
import {
  computed,
  watch,
  nextTick,
  ref,
  onBeforeUnmount,
  onMounted,
  inject,
  type Ref,
} from 'vue'
import {
  fileBasename,
  renderMarkdown,
  rewriteWorkspaceSrc,
  workspaceFileUrl,
} from '../lib/markdown'
import { renderMermaidIn } from '../lib/mermaid'

const props = defineProps<{
  source: string
  streaming?: boolean
  /** History: skip highlight + truncate long bodies (default true when not streaming). */
  lite?: boolean
}>()

const el = ref<HTMLElement | null>(null)
const localBlobs = ref<string[]>([])
const authToken = inject<Ref<string> | string | null>('authToken', null)
let hydrateTimer: ReturnType<typeof setTimeout> | null = null
let hydrateGen = 0

/** Full-screen image viewer */
const lightboxSrc = ref('')
const lightboxAlt = ref('')
const lightboxOpen = ref(false)
const lightboxIsSvg = ref(false)
/**
 * Inline mermaid/```svg must open as live DOM (not <img src=blob>).
 * Mermaid htmlLabels use foreignObject; blob+img drops them → blank white stage.
 */
const lightboxSvgHost = ref<HTMLElement | null>(null)
const lightboxStage = ref<HTMLElement | null>(null)
let lightboxSvgClone: SVGElement | null = null

/** Interactive zoom / pan (1 = fit-to-stage). */
const LB_ZOOM_MIN = 0.25
const LB_ZOOM_MAX = 8
const lbZoom = ref(1)
const lbPanX = ref(0)
const lbPanY = ref(0)
const lbZoomPct = computed(() => Math.round(lbZoom.value * 100))
const lbContentStyle = computed(() => ({
  transform: `translate(${lbPanX.value}px, ${lbPanY.value}px) scale(${lbZoom.value})`,
}))

const lbDragging = ref(false)
let lbDragMoved = false
let lbDragX = 0
let lbDragY = 0
let lbDragOriginX = 0
let lbDragOriginY = 0
let lbPointerId: number | null = null

function resetLbTransform() {
  lbZoom.value = 1
  lbPanX.value = 0
  lbPanY.value = 0
}

function clampLbZoom(z: number) {
  return Math.min(LB_ZOOM_MAX, Math.max(LB_ZOOM_MIN, z))
}

/** Zoom by factor; optional client coords for zoom-toward-cursor. */
function lbZoomBy(factor: number, clientX?: number, clientY?: number) {
  const prev = lbZoom.value
  const next = clampLbZoom(prev * factor)
  if (next === prev) return
  const stage = lightboxStage.value
  if (
    stage &&
    clientX != null &&
    clientY != null &&
    Number.isFinite(clientX) &&
    Number.isFinite(clientY)
  ) {
    const rect = stage.getBoundingClientRect()
    const cx = clientX - rect.left - rect.width / 2
    const cy = clientY - rect.top - rect.height / 2
    // Keep point under cursor stable: pan' = pan + (cx - pan) * (1 - next/prev) ... 
    // content is centered; transform origin center
    const ratio = next / prev
    lbPanX.value = cx - (cx - lbPanX.value) * ratio
    lbPanY.value = cy - (cy - lbPanY.value) * ratio
  }
  lbZoom.value = next
}

function lbZoomIn() {
  lbZoomBy(1.25)
}
function lbZoomOut() {
  lbZoomBy(1 / 1.25)
}

function onLbWheel(e: WheelEvent) {
  e.preventDefault()
  // Trackpad pinch often sends ctrlKey; treat all wheel as zoom
  const factor = e.deltaY > 0 ? 1 / 1.12 : 1.12
  lbZoomBy(factor, e.clientX, e.clientY)
}

function onLbPointerDown(e: PointerEvent) {
  if (e.button !== 0) return
  lbDragging.value = true
  lbDragMoved = false
  lbDragX = e.clientX
  lbDragY = e.clientY
  lbDragOriginX = lbPanX.value
  lbDragOriginY = lbPanY.value
  lbPointerId = e.pointerId
  try {
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  } catch {
    /* ignore */
  }
}

function onLbPointerMove(e: PointerEvent) {
  if (!lbDragging.value || lbPointerId !== e.pointerId) return
  const dx = e.clientX - lbDragX
  const dy = e.clientY - lbDragY
  if (Math.abs(dx) + Math.abs(dy) > 3) lbDragMoved = true
  lbPanX.value = lbDragOriginX + dx
  lbPanY.value = lbDragOriginY + dy
}

function onLbPointerUp(e: PointerEvent) {
  if (lbPointerId !== e.pointerId) return
  lbDragging.value = false
  lbPointerId = null
  try {
    ;(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId)
  } catch {
    /* ignore */
  }
}

function onLbDoubleClick(e: MouseEvent) {
  e.preventDefault()
  e.stopPropagation()
  if (lbZoom.value > 1.05) {
    resetLbTransform()
  } else {
    lbZoomBy(2, e.clientX, e.clientY)
  }
}

const blobCache: Map<string, string> =
  (globalThis as unknown as { __ariadneImgCache?: Map<string, string> }).__ariadneImgCache ||
  new Map()
;(globalThis as unknown as { __ariadneImgCache?: Map<string, string> }).__ariadneImgCache =
  blobCache

const useLite = computed(() => props.lite !== false && !props.streaming)

const html = computed(() =>
  renderMarkdown(props.source || '', {
    highlight: !useLite.value,
    maxSourceChars: useLite.value ? 24_000 : undefined,
  }),
)

function tokenValue(): string {
  if (!authToken) return ''
  if (typeof authToken === 'string') return authToken
  return authToken.value || ''
}

function looksLikeSvg(src: string, alt?: string): boolean {
  const a = String(alt || '')
  const s = String(src || '')
  if (/\.svg(\?|$)/i.test(a) || /\.svg(\?|$)/i.test(s)) return true
  if (/diagram\.svg/i.test(a)) return true
  // blob: from serialized inline/mermaid SVG — alt set to diagram.svg
  if (/^blob:/i.test(s) && /svg/i.test(a)) return true
  return false
}

function clearLightboxSvg() {
  if (lightboxSvgHost.value) lightboxSvgHost.value.innerHTML = ''
  lightboxSvgClone = null
}

function openLightbox(src: string, alt?: string, opts?: { svg?: boolean }) {
  if (!src || src.startsWith('data:')) return
  clearLightboxSvg()
  resetLbTransform()
  lightboxSrc.value = src
  lightboxAlt.value = alt || '图片'
  lightboxIsSvg.value = opts?.svg === true || looksLikeSvg(src, alt)
  lightboxOpen.value = true
}

/**
 * Workspace / remote .svg files: do not zoom via <img src=blob>.
 * Auth'd API URLs fail in bare <img>; complex SVG often paints blank/blurry as image.
 * Fetch text → parse DOM → same live-SVG path as mermaid.
 */
async function openSvgFromUrl(
  url: string,
  alt?: string,
  apiUrl?: string,
): Promise<void> {
  const label = alt || 'diagram.svg'
  lightboxAlt.value = label
  lightboxIsSvg.value = true
  // Open shell immediately so user sees stage (not blank click)
  resetLbTransform()
  lightboxSrc.value = ''
  clearLightboxSvg()
  lightboxOpen.value = true
  await nextTick()

  const token = tokenValue()
  let text = ''
  try {
    // Prefer API path with Bearer (workspace files)
    const fetchUrl = rewriteWorkspaceSrc(apiUrl || url)
    if (fetchUrl.startsWith('/api/workspace/file')) {
      const r = await fetch(fetchUrl, {
        headers: token ? { Authorization: 'Bearer ' + token } : {},
      })
      if (!r.ok) throw new Error(`load svg ${r.status}`)
      text = await r.text()
    } else if (/^blob:/i.test(url)) {
      const r = await fetch(url)
      if (!r.ok) throw new Error(`blob svg ${r.status}`)
      text = await r.text()
    } else if (/^data:image\/svg\+xml/i.test(url)) {
      const comma = url.indexOf(',')
      const raw = comma >= 0 ? url.slice(comma + 1) : ''
      text = /;base64,/i.test(url.slice(0, comma + 1))
        ? atob(raw)
        : decodeURIComponent(raw)
    } else {
      // Fallback: try as-is (may fail without CORS/auth)
      const r = await fetch(url)
      if (!r.ok) throw new Error(`fetch svg ${r.status}`)
      text = await r.text()
    }
  } catch {
    // Last resort: show as <img> so download bar still works
    openLightbox(url.startsWith('blob:') || url.startsWith('/api/') ? url : rewriteWorkspaceSrc(apiUrl || url), label, {
      svg: true,
    })
    return
  }

  text = String(text || '').trim()
  // Strip XML declaration for DOMParser
  text = text.replace(/^\s*<\?xml[^?]*\?>\s*/i, '')
  if (!/<svg[\s>]/i.test(text)) {
    // Typed blob fallback (auth already resolved into text if possible)
    const u = URL.createObjectURL(
      new Blob([text || ''], { type: 'image/svg+xml;charset=utf-8' }),
    )
    localBlobs.value.push(u)
    openLightbox(u, label, { svg: true })
    return
  }

  const parsed = new DOMParser().parseFromString(text, 'image/svg+xml')
  const svgEl = parsed.querySelector('svg')
  if (!svgEl || parsed.querySelector('parsererror')) {
    const u = URL.createObjectURL(
      new Blob([text], { type: 'image/svg+xml;charset=utf-8' }),
    )
    localBlobs.value.push(u)
    openLightbox(u, label, { svg: true })
    return
  }
  // Import into current document so gradients/filters resolve
  const imported = document.importNode(svgEl, true) as SVGElement
  await mountSvgInLightbox(imported, label)
}

/** Parse viewBox / width / height into a numeric content size. */
function svgNaturalSize(svg: SVGElement): { w: number; h: number } {
  const vb = (svg.getAttribute('viewBox') || '').trim().split(/[\s,]+/).map(Number)
  if (vb.length === 4 && vb[2] > 0 && vb[3] > 0) {
    return { w: vb[2], h: vb[3] }
  }
  const attrW = parseFloat(svg.getAttribute('width') || '')
  const attrH = parseFloat(svg.getAttribute('height') || '')
  if (attrW > 0 && attrH > 0) return { w: attrW, h: attrH }
  try {
    const box = (svg as SVGGraphicsElement).getBBox?.()
    if (box && box.width > 0 && box.height > 0) {
      return { w: box.width, h: box.height }
    }
  } catch {
    /* not laid out */
  }
  const r = svg.getBoundingClientRect()
  if (r.width > 0 && r.height > 0) return { w: r.width, h: r.height }
  return { w: 800, h: 500 }
}

/** Fit an SVG element into the lightbox host at base (zoom=1) size. */
async function mountSvgInLightbox(svg: SVGElement, alt?: string) {
  lightboxSrc.value = ''
  lightboxAlt.value = alt || 'diagram.svg'
  lightboxIsSvg.value = true
  lightboxOpen.value = true
  await nextTick()
  const host = lightboxSvgHost.value
  if (!host) return

  const clone = svg.cloneNode(true) as SVGElement
  if (!clone.getAttribute('xmlns')) {
    clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
  }

  // Ensure viewBox so width/height attrs scale content (not crop)
  if (!clone.getAttribute('viewBox')) {
    try {
      const box = (svg as SVGGraphicsElement).getBBox?.()
      if (box && box.width > 0 && box.height > 0) {
        clone.setAttribute(
          'viewBox',
          `${box.x} ${box.y} ${box.width} ${box.height}`,
        )
      }
    } catch {
      /* not laid out */
    }
  }
  if (!clone.getAttribute('viewBox')) {
    const { w, h } = svgNaturalSize(svg)
    clone.setAttribute('viewBox', `0 0 ${w} ${h}`)
  }

  // Base fit size; further zoom is CSS transform on .lb-content
  const { w: contentW, h: contentH } = svgNaturalSize(clone)
  const aspect = contentW / Math.max(contentH, 1)
  const stage = lightboxStage.value
  const stageW = stage?.clientWidth || Math.min(window.innerWidth * 0.94, 1440)
  const stageH = stage?.clientHeight || Math.max(200, window.innerHeight - 150)
  const maxW = Math.max(120, stageW - 24)
  const maxH = Math.max(80, stageH - 24)
  let fitW = maxW
  let fitH = fitW / aspect
  if (fitH > maxH) {
    fitH = maxH
    fitW = fitH * aspect
  }
  fitW = Math.max(120, Math.round(fitW))
  fitH = Math.max(80, Math.round(fitH))

  clone.setAttribute('width', String(fitW))
  clone.setAttribute('height', String(fitH))
  clone.setAttribute('preserveAspectRatio', 'xMidYMid meet')
  clone.style.width = `${fitW}px`
  clone.style.height = `${fitH}px`
  clone.style.maxWidth = 'none'
  clone.style.maxHeight = 'none'
  clone.style.display = 'block'
  host.innerHTML = ''
  host.appendChild(clone)
  lightboxSvgClone = clone
}

/** Mount a deep-cloned SVG into the lightbox (keeps foreignObject / styles). */
async function openSvgElementLightbox(svg: SVGElement, alt?: string) {
  clearLightboxSvg()
  resetLbTransform()
  await mountSvgInLightbox(svg, alt)
}

function closeLightbox() {
  lightboxOpen.value = false
  lightboxSrc.value = ''
  lightboxAlt.value = ''
  lightboxIsSvg.value = false
  clearLightboxSvg()
  resetLbTransform()
  lbDragging.value = false
  lbPointerId = null
}

function onLightboxKey(e: KeyboardEvent) {
  if (!lightboxOpen.value) return
  if (e.key === 'Escape') {
    closeLightbox()
    return
  }
  if (e.key === '+' || e.key === '=') {
    e.preventDefault()
    lbZoomIn()
  } else if (e.key === '-' || e.key === '_') {
    e.preventDefault()
    lbZoomOut()
  } else if (e.key === '0') {
    e.preventDefault()
    resetLbTransform()
  }
}

async function downloadAuthUrl(apiUrl: string, nameHint?: string) {
  const name = nameHint || fileBasename(apiUrl) || 'download'
  // Already-hydrated image blob or data URL
  if (/^(?:blob:|data:)/i.test(apiUrl)) {
    const a = document.createElement('a')
    a.href = apiUrl
    a.download = name
    document.body.appendChild(a)
    a.click()
    a.remove()
    return
  }
  const token = tokenValue()
  const url = rewriteWorkspaceSrc(apiUrl)
  if (!url.startsWith('/api/workspace/file')) {
    // External link — open normally
    window.open(apiUrl, '_blank', 'noopener,noreferrer')
    return
  }
  const r = await fetch(url, {
    headers: token ? { Authorization: 'Bearer ' + token } : {},
  })
  if (!r.ok) throw new Error(`下载失败 ${r.status}`)
  const blob = await r.blob()
  const objectUrl = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = objectUrl
  a.download = nameHint || fileBasename(url) || 'download'
  // Try Content-Disposition filename
  const cd = r.headers.get('content-disposition') || ''
  const m = /filename\*?=(?:UTF-8''|")?([^\";]+)/i.exec(cd)
  if (m) {
    try {
      a.download = decodeURIComponent(m[1].replace(/"/g, '').trim())
    } catch {
      a.download = m[1].replace(/"/g, '').trim()
    }
  }
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(objectUrl), 2000)
}

function svgElementToObjectUrl(svg: SVGElement): string {
  const clone = svg.cloneNode(true) as SVGElement
  if (!clone.getAttribute('xmlns')) {
    clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
  }
  // Ensure readable size when downloaded / opened as file
  if (!clone.getAttribute('width') && !clone.getAttribute('viewBox')) {
    try {
      const box = (svg as SVGGraphicsElement).getBBox?.()
      if (box && box.width && box.height) {
        clone.setAttribute(
          'viewBox',
          `${box.x} ${box.y} ${box.width} ${box.height}`,
        )
      }
    } catch {
      /* getBBox can throw if not in DOM */
    }
  }
  // Prefix XML for better consumer support; foreignObject HTML may still be imperfect offline
  let xml = new XMLSerializer().serializeToString(clone)
  if (!/^\s*<\?xml/i.test(xml)) {
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml
  }
  const blob = new Blob([xml], { type: 'image/svg+xml;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  localBlobs.value.push(url)
  return url
}

function wireImageZoom(root: HTMLElement) {
  root.querySelectorAll<HTMLImageElement>('img').forEach((img) => {
    if (img.dataset.zoomWired === '1') return
    if (img.classList.contains('workspace-img-failed')) return
    img.dataset.zoomWired = '1'
    img.classList.add('md-zoomable')
    if (!img.title) img.title = '点击放大'
    img.addEventListener('click', (ev) => {
      ev.preventDefault()
      ev.stopPropagation()
      const src = img.currentSrc || img.src || img.getAttribute('data-workspace-src') || ''
      if (!src) return
      const api = img.getAttribute('data-workspace-src') || ''
      const alt =
        img.alt ||
        (api && /\.svg(\?|$)/i.test(api) ? fileBasename(api) : '') ||
        '图片'
      const isSvg =
        /\.svg(\?|$)/i.test(api) ||
        /\.svg(\?|$)/i.test(src) ||
        /\.svg(\?|$)/i.test(alt) ||
        (img.src && /image\/svg/i.test(img.src)) ||
        !!(img.src && img.src.startsWith('blob:') && /\.svg/i.test(api || alt))
      // Workspace / file SVGs → live DOM lightbox (auth + sharp render)
      if (isSvg) {
        void openSvgFromUrl(src, alt, api || undefined)
        return
      }
      openLightbox(src, alt, { svg: false })
    })
  })

  // Inline ```svg fences and mermaid-rendered SVG (not <img>)
  root
    .querySelectorAll<SVGElement>('.md-svg > svg, .md-mermaid svg, svg.md-zoomable')
    .forEach((svg) => {
      if (svg.dataset.zoomWired === '1') return
      svg.dataset.zoomWired = '1'
      svg.classList.add('md-zoomable-svg')
      if (!svg.getAttribute('role')) svg.setAttribute('role', 'img')
      if (!svg.getAttribute('title')) svg.setAttribute('title', '点击放大')
      const wrap = svg.closest('.md-svg, .md-mermaid') as HTMLElement | null
      if (wrap) {
        wrap.classList.add('md-zoomable-svg-wrap')
        if (!wrap.title) wrap.title = '点击放大'
      }
      const open = (ev: Event) => {
        ev.preventDefault()
        ev.stopPropagation()
        void openSvgElementLightbox(svg, 'diagram.svg')
      }
      svg.addEventListener('click', open)
      wrap?.addEventListener('click', (ev) => {
        // Only if click landed on the wrap padding / svg
        if (ev.target === wrap || (ev.target as Node).nodeName === 'svg' || svg.contains(ev.target as Node)) {
          open(ev)
        }
      })
    })
}

function wireFileDownloads(root: HTMLElement) {
  root.querySelectorAll<HTMLAnchorElement>('a').forEach((a) => {
    const href =
      a.getAttribute('data-workspace-href') ||
      a.getAttribute('href') ||
      ''
    const isWs =
      a.classList.contains('md-ws-file') ||
      href.startsWith('/api/workspace/file') ||
      /^\/?workspace\//i.test(href)
    if (!isWs) return
    if (a.dataset.dlWired === '1') return
    a.dataset.dlWired = '1'
    a.classList.add('md-ws-file')
    // Resolve sandbox paths still in href
    let apiHref = href
    if (!apiHref.startsWith('/api/workspace/file')) {
      apiHref = workspaceFileUrl(href) || rewriteWorkspaceSrc(href)
    } else {
      apiHref = rewriteWorkspaceSrc(apiHref)
    }
    if (apiHref.startsWith('/api/workspace/file')) {
      a.setAttribute('data-workspace-href', apiHref)
      a.setAttribute('href', apiHref)
    }
    if (!a.title) a.title = '点击下载'
    a.addEventListener('click', (ev) => {
      ev.preventDefault()
      ev.stopPropagation()
      const u = a.getAttribute('data-workspace-href') || a.getAttribute('href') || ''
      const name =
        a.getAttribute('download') ||
        fileBasename(u) ||
        (a.textContent || '').replace(/^📎\s*/, '').trim() ||
        'download'
      void downloadAuthUrl(u, name).catch((err) => {
        a.classList.add('md-ws-file-fail')
        a.title = err instanceof Error ? err.message : '下载失败'
      })
    })
  })
}

async function hydrateWorkspaceImages() {
  const root = el.value
  if (!root) return
  const gen = ++hydrateGen
  const token = tokenValue()
  const imgs = [...root.querySelectorAll<HTMLImageElement>('img')]
  await Promise.all(
    imgs.map(async (img) => {
      if (gen !== hydrateGen) return
      let src = img.getAttribute('src') || img.getAttribute('data-workspace-src') || ''
      if (src.startsWith('blob:')) {
        const orig = img.getAttribute('data-workspace-src') || ''
        if (!orig) {
          wireImageZoom(root)
          return
        }
        src = orig
      }
      const rewritten = rewriteWorkspaceSrc(src)
      if (!rewritten.startsWith('/api/workspace/file')) {
        // External / data images — still zoomable
        return
      }
      img.setAttribute('data-workspace-src', rewritten)
      if (img.dataset.apiSrc !== rewritten) {
        img.dataset.hydrated = '0'
        img.dataset.apiSrc = rewritten
      }
      if (img.dataset.hydrated === '1' && img.src.startsWith('blob:')) return
      try {
        const cached = blobCache.get(rewritten)
        if (cached) {
          img.src = cached
          img.dataset.hydrated = '1'
          img.classList.add('workspace-img')
          if (!img.alt) img.alt = '图片'
          return
        }
        const r = await fetch(rewritten, {
          headers: token ? { Authorization: 'Bearer ' + token } : {},
        })
        if (gen !== hydrateGen) return
        if (!r.ok) {
          img.alt = (img.alt || '图片') + ` (加载失败 ${r.status})`
          img.classList.add('workspace-img-failed')
          img.removeAttribute('src')
          return
        }
        const raw = await r.blob()
        // Force correct MIME so <img> / blob re-fetch treat SVG as vector
        const isSvgPath = /\.svg(\?|$)/i.test(rewritten)
        const blob =
          isSvgPath && !/^image\/svg\+xml/i.test(raw.type || '')
            ? new Blob([await raw.arrayBuffer()], {
                type: 'image/svg+xml;charset=utf-8',
              })
            : raw
        const url = URL.createObjectURL(blob)
        blobCache.set(rewritten, url)
        localBlobs.value.push(url)
        img.src = url
        img.dataset.hydrated = '1'
        img.classList.add('workspace-img')
        img.classList.remove('workspace-img-failed')
        if (!img.alt) img.alt = isSvgPath ? fileBasename(rewritten) || 'diagram.svg' : '图片'
      } catch {
        if (gen !== hydrateGen) return
        img.alt = (img.alt || '图片') + ' (加载失败)'
        img.classList.add('workspace-img-failed')
      }
    }),
  )
  if (gen === hydrateGen && root) {
    wireImageZoom(root)
    wireFileDownloads(root)
  }
}

function scheduleHydrate(immediate = false) {
  if (hydrateTimer) clearTimeout(hydrateTimer)
  const delay = immediate || props.streaming ? 0 : 16
  hydrateTimer = setTimeout(() => {
    void hydrateWorkspaceImages()
  }, delay)
}

function afterRender() {
  const root = el.value
  if (!root) return
  root.querySelectorAll('table').forEach((table) => {
    if (table.parentElement?.classList.contains('table-wrap')) return
    const wrap = document.createElement('div')
    wrap.className = 'table-wrap'
    table.parentNode?.insertBefore(wrap, table)
    wrap.appendChild(table)
  })
  // Wire file links even before image hydrate finishes
  wireFileDownloads(root)
  scheduleHydrate(true)
  // Inline SVG fences are present immediately (history reload path)
  wireImageZoom(root)
  // Streaming: slight debounce so incomplete fences fail soft; history: immediate
  const mermaidDelay = props.streaming ? 120 : 0
  window.setTimeout(() => {
    void renderMermaidIn(el.value).then(() => {
      // Mermaid injects <svg> after render — re-wire zoom
      if (el.value) wireImageZoom(el.value)
    })
  }, mermaidDelay)
}

watch(
  html,
  async () => {
    await nextTick()
    afterRender()
  },
  { flush: 'post' },
)

watch(
  () => props.source,
  async () => {
    await nextTick()
    afterRender()
  },
)

onMounted(() => {
  afterRender()
  window.addEventListener('keydown', onLightboxKey)
})

onBeforeUnmount(() => {
  hydrateGen++
  if (hydrateTimer) clearTimeout(hydrateTimer)
  localBlobs.value = []
  window.removeEventListener('keydown', onLightboxKey)
  closeLightbox()
})
</script>

<template>
  <div
    ref="el"
    class="md-body"
    :class="{ 'streaming-answer': streaming && source, lite: useLite }"
    v-html="html"
  />

  <Teleport to="body">
    <div
      v-if="lightboxOpen"
      class="img-lightbox"
      :class="{ 'lb-svg': lightboxIsSvg, 'lb-zoomed': lbZoom > 1.02 }"
      role="dialog"
      aria-modal="true"
      aria-label="图片预览"
      @click.self="closeLightbox"
    >
      <button type="button" class="lb-close" title="关闭 (Esc)" @click="closeLightbox">×</button>
      <div
        ref="lightboxStage"
        class="lb-stage"
        :class="{ 'lb-stage-svg': lightboxIsSvg, 'lb-grabbing': lbDragging }"

        title="滚轮缩放 · 拖拽平移 · 双击切换"
        @click.stop="
          (e) => {
            // Ignore pure click after drag
            if (lbDragMoved) {
              e.preventDefault()
              lbDragMoved = false
            }
          }
        "
        @wheel="onLbWheel"
        @pointerdown="onLbPointerDown"
        @pointermove="onLbPointerMove"
        @pointerup="onLbPointerUp"
        @pointercancel="onLbPointerUp"
        @dblclick="onLbDoubleClick"
      >
        <div class="lb-content" :style="lbContentStyle">
          <!-- Raster / non-SVG images -->
          <img
            v-if="lightboxSrc"
            class="lb-img"
            :src="lightboxSrc"
            :alt="lightboxAlt"
            draggable="false"
          />
          <!-- Inline mermaid / workspace SVG — live DOM -->
          <div
            v-show="!lightboxSrc"
            ref="lightboxSvgHost"
            class="lb-svg-host"
            role="img"
            :aria-label="lightboxAlt"
          />
        </div>
      </div>
      <div class="lb-bar" @click.stop @pointerdown.stop>
        <span class="lb-alt" :title="lightboxAlt">{{ lightboxAlt }}</span>
        <span class="lb-zoom-tools">
          <button type="button" class="lb-tool" title="缩小 (−)" @click="lbZoomOut">−</button>
          <button
            type="button"
            class="lb-tool lb-pct"
            title="重置适应 (0)"
            @click="resetLbTransform"
          >
            {{ lbZoomPct }}%
          </button>
          <button type="button" class="lb-tool" title="放大 (+)" @click="lbZoomIn">+</button>
        </span>
        <button
          type="button"
          class="lb-dl"
          @click="
            () => {
              let name =
                lightboxAlt && lightboxAlt !== '图片' ? lightboxAlt : 'image.png'
              if (lightboxSvgClone) {
                try {
                  const url = svgElementToObjectUrl(lightboxSvgClone)
                  void downloadAuthUrl(url, /\.svg$/i.test(name) ? name : 'diagram.svg')
                } catch {
                  /* ignore */
                }
                return
              }
              if (/\.svg$/i.test(name) === false && /^blob:/i.test(lightboxSrc)) {
                if (!/\.(png|jpe?g|gif|webp)$/i.test(name)) name = 'diagram.svg'
              }
              void downloadAuthUrl(lightboxSrc, name).catch(() => {
                const a = document.createElement('a')
                a.href = lightboxSrc
                a.download = name
                a.target = '_blank'
                a.click()
              })
            }
          "
        >
          下载
        </button>
      </div>
    </div>
  </Teleport>
</template>

<style>
.md-body img.workspace-img,
.md-body img[src^='blob:'],
.md-body img.md-zoomable {
  display: block;
  max-width: min(100%, 720px);
  height: auto;
  margin: 0.75em 0;
  border-radius: 12px;
  border: 1px solid var(--line);
  background: var(--bg-3);
}
.md-body img.md-zoomable {
  cursor: zoom-in;
}
.md-body img.md-zoomable:hover {
  outline: 2px solid color-mix(in srgb, var(--blue) 45%, transparent);
  outline-offset: 2px;
}
.md-body img.workspace-img-failed {
  display: block;
  margin: 0.5em 0;
  padding: 8px 12px;
  border-radius: 8px;
  border: 1px dashed var(--warn, #ffad1f);
  color: var(--dim);
  font-size: 13px;
  max-width: 100%;
  cursor: default;
}
.md-body a.md-ws-file {
  display: inline-flex;
  align-items: center;
  gap: 0.25em;
  padding: 0.12em 0.45em;
  border-radius: 8px;
  border: 1px solid var(--line);
  background: var(--bg-3);
  color: var(--blue);
  text-decoration: none;
  font-size: 0.92em;
  cursor: pointer;
  max-width: 100%;
  word-break: break-all;
}
.md-body a.md-ws-file:hover {
  background: var(--bg-hover);
  text-decoration: underline;
}
.md-body a.md-ws-file-fail {
  border-color: var(--warn, #ffad1f);
  color: var(--warn, #c77);
}
.md-body .md-truncated-note {
  color: var(--muted);
  font-size: 13px;
}
.md-body .md-mermaid {
  margin: 0.9em 0;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--bg-3);
  overflow-x: auto;
}
.md-body .md-mermaid pre.mermaid {
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
  font-size: 13px;
  line-height: 1.45;
}
.md-body .md-mermaid svg {
  display: block;
  max-width: 100%;
  height: auto;
  margin: 0 auto;
}
.md-body .md-svg {
  margin: 0.9em 0;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--bg-3);
  overflow-x: auto;
  text-align: center;
}
.md-body .md-svg svg {
  max-width: 100%;
  height: auto;
}
.md-body .md-zoomable-svg-wrap,
.md-body svg.md-zoomable-svg {
  cursor: zoom-in;
}
.md-body .md-zoomable-svg-wrap:hover {
  outline: 2px solid color-mix(in srgb, var(--blue) 45%, transparent);
  outline-offset: 2px;
}
.md-body .md-svg.md-zoomable-svg-wrap,
.md-body .md-mermaid.md-zoomable-svg-wrap {
  cursor: zoom-in;
}

/* Full-screen image lightbox */
.img-lightbox {
  position: fixed;
  inset: 0;
  z-index: 9000;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 48px 16px 80px;
  background: rgba(0, 0, 0, 0.82);
  backdrop-filter: blur(8px);
  cursor: zoom-out;
  user-select: none;
}
.img-lightbox .lb-stage {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  width: min(96vw, 1440px);
  height: min(86vh, 920px);
  max-height: calc(100vh - 130px);
  border-radius: 14px;
  /* Unified dark graphite board — no harsh white paper */
  background:
    radial-gradient(ellipse at 50% 40%, #1c1f28 0%, #0e1016 70%);
  border: 1px solid rgba(255, 255, 255, 0.08);
  box-shadow: 0 16px 56px rgba(0, 0, 0, 0.55);
  overflow: hidden;
  cursor: grab;
  touch-action: none;
  box-sizing: border-box;
}
.img-lightbox .lb-stage.lb-grabbing {
  cursor: grabbing;
}
.img-lightbox .lb-stage-svg,
.img-lightbox.lb-svg .lb-stage {
  /* same dark board for SVG / mermaid (architecture diagrams are often dark-themed) */
  background:
    radial-gradient(ellipse at 50% 40%, #1c1f28 0%, #0e1016 70%);
}
.img-lightbox .lb-content {
  transform-origin: center center;
  will-change: transform;
  display: flex;
  align-items: center;
  justify-content: center;
  max-width: none;
  max-height: none;
}
.img-lightbox .lb-img {
  max-width: min(90vw, 1360px);
  max-height: min(80vh, 860px);
  width: auto;
  height: auto;
  object-fit: contain;
  border-radius: 4px;
  background: transparent;
  display: block;
  pointer-events: none;
}
.img-lightbox .lb-svg-host {
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
}
.img-lightbox .lb-svg-host > svg {
  display: block;
  max-width: none;
  max-height: none;
  flex-shrink: 0;
  pointer-events: none;
}
.img-lightbox .lb-close {
  position: absolute;
  top: 12px;
  right: 16px;
  width: 40px;
  height: 40px;
  border: 0;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.12);
  color: #fff;
  font-size: 28px;
  line-height: 1;
  cursor: pointer;
  z-index: 2;
}
.img-lightbox .lb-close:hover {
  background: rgba(255, 255, 255, 0.22);
}
.img-lightbox .lb-bar {
  position: absolute;
  bottom: 16px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 10px;
  max-width: 94vw;
  padding: 8px 12px 8px 16px;
  border-radius: 999px;
  background: rgba(12, 14, 20, 0.88);
  border: 1px solid rgba(255, 255, 255, 0.1);
  color: #eee;
  font-size: 13px;
  cursor: default;
  z-index: 2;
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.4);
}
.img-lightbox .lb-alt {
  max-width: min(36vw, 280px);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  opacity: 0.85;
}
.img-lightbox .lb-zoom-tools {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0 4px;
  border-left: 1px solid rgba(255, 255, 255, 0.12);
  border-right: 1px solid rgba(255, 255, 255, 0.12);
  margin: 0 2px;
}
.img-lightbox .lb-tool {
  min-width: 32px;
  height: 30px;
  border: 0;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.08);
  color: #f3f4f6;
  font-size: 16px;
  font-weight: 600;
  line-height: 1;
  cursor: pointer;
  padding: 0 8px;
}
.img-lightbox .lb-tool:hover {
  background: rgba(255, 255, 255, 0.16);
}
.img-lightbox .lb-tool.lb-pct {
  min-width: 52px;
  font-size: 12px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  opacity: 0.95;
}
.img-lightbox .lb-dl {
  border: 0;
  border-radius: 999px;
  padding: 6px 14px;
  background: #3b82f6;
  color: #fff;
  font-size: 13px;
  cursor: pointer;
}
.img-lightbox .lb-dl:hover {
  filter: brightness(1.08);
}
</style>

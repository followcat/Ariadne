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
import { renderMarkdown, rewriteWorkspaceSrc } from '../lib/markdown'

const props = defineProps<{
  source: string
  streaming?: boolean
  /** History: skip highlight + soft long bodies (default true when not streaming). */
  lite?: boolean
}>()

const el = ref<HTMLElement | null>(null)
const localBlobs = ref<string[]>([])
const authToken = inject<Ref<string> | string | null>('authToken', null)
let hydrateTimer: ReturnType<typeof setTimeout> | null = null
let hydrateGen = 0

const blobCache: Map<string, string> =
  (globalThis as unknown as { __ariadneImgCache?: Map<string, string> }).__ariadneImgCache ||
  new Map()
;(globalThis as unknown as { __ariadneImgCache?: Map<string, string> }).__ariadneImgCache =
  blobCache

const useLite = computed(() => props.lite !== false && !props.streaming)

const html = computed(() =>
  renderMarkdown(props.source || '', {
    highlight: !useLite.value,
    // Soft-cap long thrash dumps only; keep room for image markdown near the top.
    maxSourceChars: useLite.value ? 24_000 : undefined,
  }),
)

function tokenValue(): string {
  if (!authToken) return ''
  if (typeof authToken === 'string') return authToken
  return authToken.value || ''
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
      // Always re-scope atelier query (stale session in cached HTML).
      let src = img.getAttribute('src') || img.getAttribute('data-workspace-src') || ''
      if (src.startsWith('blob:')) {
        // Already hydrated; re-check data-workspace-src for session switch.
        const orig = img.getAttribute('data-workspace-src') || ''
        if (!orig) return
        src = orig
      }
      const rewritten = rewriteWorkspaceSrc(src)
      if (!rewritten.startsWith('/api/workspace/file')) return
      img.setAttribute('data-workspace-src', rewritten)
      // Reset hydrated when URL scope changed (main ↔ branch).
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
        const blob = await r.blob()
        const url = URL.createObjectURL(blob)
        blobCache.set(rewritten, url)
        localBlobs.value.push(url)
        img.src = url
        img.dataset.hydrated = '1'
        img.classList.add('workspace-img')
        img.classList.remove('workspace-img-failed')
        if (!img.alt) img.alt = '图片'
      } catch {
        if (gen !== hydrateGen) return
        img.alt = (img.alt || '图片') + ' (加载失败)'
        img.classList.add('workspace-img-failed')
      }
    }),
  )
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
  scheduleHydrate(true)
}

watch(
  html,
  async () => {
    await nextTick()
    afterRender()
  },
  { flush: 'post' },
)

// History reload can reuse component instances — re-hydrate when source flips.
watch(
  () => props.source,
  async () => {
    await nextTick()
    afterRender()
  },
)

onMounted(() => {
  afterRender()
})

onBeforeUnmount(() => {
  hydrateGen++
  if (hydrateTimer) clearTimeout(hydrateTimer)
  localBlobs.value = []
})
</script>

<template>
  <div
    ref="el"
    class="md-body"
    :class="{ 'streaming-answer': streaming && source, lite: useLite }"
    v-html="html"
  />
</template>

<style>
.md-body img.workspace-img,
.md-body img[src^="blob:"] {
  display: block;
  max-width: min(100%, 720px);
  height: auto;
  margin: 0.75em 0;
  border-radius: 12px;
  border: 1px solid var(--line);
  background: var(--bg-3);
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
}
.md-body .md-truncated-note {
  color: var(--muted);
  font-size: 13px;
}
</style>

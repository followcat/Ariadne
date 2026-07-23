<script setup lang="ts">
import { computed, watch, nextTick, ref, onBeforeUnmount, inject, type Ref } from 'vue'
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
/** Defer full image hydrate until idle so text paints first. */
let hydrateTimer: ReturnType<typeof setTimeout> | null = null

const blobCache: Map<string, string> =
  (globalThis as unknown as { __ariadneImgCache?: Map<string, string> }).__ariadneImgCache ||
  new Map()
;(globalThis as unknown as { __ariadneImgCache?: Map<string, string> }).__ariadneImgCache =
  blobCache

const useLite = computed(() => props.lite !== false && !props.streaming)

const html = computed(() =>
  renderMarkdown(props.source || '', {
    highlight: !useLite.value,
    // Long thrash dumps (minified JS) made history switch multi-second.
    maxSourceChars: useLite.value ? 12_000 : undefined,
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
  const token = tokenValue()
  const imgs = [...root.querySelectorAll<HTMLImageElement>('img')]
  await Promise.all(
    imgs.map(async (img) => {
      if (img.dataset.hydrated === '1') return
      let src = img.getAttribute('src') || ''
      const rewritten = rewriteWorkspaceSrc(src)
      if (rewritten !== src) {
        src = rewritten
        img.setAttribute('src', src)
      }
      if (!src.startsWith('/api/workspace/file')) return
      try {
        const cached = blobCache.get(src)
        if (cached) {
          img.src = cached
          img.dataset.hydrated = '1'
          img.classList.add('workspace-img')
          if (!img.alt) img.alt = '图片'
          return
        }
        const r = await fetch(src, {
          headers: token ? { Authorization: 'Bearer ' + token } : {},
        })
        if (!r.ok) {
          img.alt = (img.alt || '图片') + ` (加载失败 ${r.status})`
          img.classList.add('workspace-img-failed')
          return
        }
        const blob = await r.blob()
        const url = URL.createObjectURL(blob)
        blobCache.set(src, url)
        localBlobs.value.push(url)
        img.src = url
        img.dataset.hydrated = '1'
        img.classList.add('workspace-img')
        if (!img.alt) img.alt = '图片'
      } catch {
        img.alt = (img.alt || '图片') + ' (加载失败)'
        img.classList.add('workspace-img-failed')
      }
    }),
  )
}

function scheduleHydrate() {
  if (hydrateTimer) clearTimeout(hydrateTimer)
  // Let text paint first; images fill in shortly after.
  hydrateTimer = setTimeout(() => {
    void hydrateWorkspaceImages()
  }, props.streaming ? 0 : 40)
}

watch(
  html,
  async () => {
    await nextTick()
    const root = el.value
    if (!root) return
    root.querySelectorAll('table').forEach((table) => {
      if (table.parentElement?.classList.contains('table-wrap')) return
      const wrap = document.createElement('div')
      wrap.className = 'table-wrap'
      table.parentNode?.insertBefore(wrap, table)
      wrap.appendChild(table)
    })
    scheduleHydrate()
  },
  { flush: 'post' },
)

onBeforeUnmount(() => {
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

<script setup lang="ts">
import { computed, watch, nextTick, ref, onBeforeUnmount, inject, type Ref } from 'vue'
import { renderMarkdown, rewriteWorkspaceSrc } from '../lib/markdown'

const props = defineProps<{
  source: string
  streaming?: boolean
}>()

const el = ref<HTMLElement | null>(null)
const blobUrls = ref<string[]>([])
const authToken = inject<Ref<string> | string | null>('authToken', null)

const html = computed(() => renderMarkdown(props.source || ''))

function tokenValue(): string {
  if (!authToken) return ''
  if (typeof authToken === 'string') return authToken
  return authToken.value || ''
}

function revokeBlobs() {
  for (const u of blobUrls.value) URL.revokeObjectURL(u)
  blobUrls.value = []
}

/**
 * Workspace files require Authorization. <img src> cannot send Bearer headers,
 * so we fetch as blob and swap to an object URL.
 */
async function hydrateWorkspaceImages() {
  const root = el.value
  if (!root) return
  const token = tokenValue()
  const imgs = root.querySelectorAll<HTMLImageElement>('img')
  for (const img of imgs) {
    if (img.dataset.hydrated === '1') continue
    let src = img.getAttribute('src') || ''
    // Safety net: markdown may still emit raw /workspace/… if purify config drifts
    const rewritten = rewriteWorkspaceSrc(src)
    if (rewritten !== src) {
      src = rewritten
      img.setAttribute('src', src)
    }
    if (!src.startsWith('/api/workspace/file')) continue
    try {
      const r = await fetch(src, {
        headers: token ? { Authorization: 'Bearer ' + token } : {},
      })
      if (!r.ok) {
        img.alt = (img.alt || '走势图') + ` (加载失败 ${r.status})`
        img.classList.add('workspace-img-failed')
        continue
      }
      const blob = await r.blob()
      const url = URL.createObjectURL(blob)
      blobUrls.value.push(url)
      img.src = url
      img.dataset.hydrated = '1'
      img.classList.add('workspace-img')
      if (!img.alt) img.alt = '走势图'
    } catch {
      img.alt = (img.alt || '走势图') + ' (加载失败)'
      img.classList.add('workspace-img-failed')
    }
  }
}

// Wrap tables + hydrate auth-gated workspace images
watch(
  html,
  async () => {
    revokeBlobs()
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
    await hydrateWorkspaceImages()
  },
  { flush: 'post' },
)

onBeforeUnmount(() => revokeBlobs())
</script>

<template>
  <div
    ref="el"
    class="md-body"
    :class="{ 'streaming-answer': streaming && source }"
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
</style>

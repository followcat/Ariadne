<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api/client'

type Entry = {
  name: string
  path: string
  kind: 'file' | 'dir'
  size: number
  mtime: number
}

const props = defineProps<{
  token: string
  active: boolean
  /** Bump after agent turns so listing picks up new files. */
  refreshKey?: number
}>()

const cwd = ref('/workspace')
const parent = ref<string | null>(null)
const entries = ref<Entry[]>([])
const loading = ref(false)
const err = ref('')
const selected = ref<string | null>(null)
const previewText = ref('')
const previewBinary = ref(false)
const previewTruncated = ref(false)
const previewName = ref('')
const previewImgUrl = ref('')
const blobUrls = ref<string[]>([])

const crumbs = computed(() => {
  const p = cwd.value.replace(/^\/workspace\/?/, '')
  if (!p) return [{ label: 'workspace', path: '/workspace' }]
  const parts = p.split('/').filter(Boolean)
  const out = [{ label: 'workspace', path: '/workspace' }]
  let acc = '/workspace'
  for (const part of parts) {
    acc += '/' + part
    out.push({ label: part, path: acc })
  }
  return out
})

function fmtSize(n: number): string {
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / (1024 * 1024)).toFixed(1) + ' MB'
}

function isImage(name: string): boolean {
  return /\.(png|jpe?g|gif|webp|svg)$/i.test(name)
}

function revokeBlobs() {
  for (const u of blobUrls.value) URL.revokeObjectURL(u)
  blobUrls.value = []
  previewImgUrl.value = ''
}

async function loadDir(path = cwd.value) {
  if (!props.token) return
  loading.value = true
  err.value = ''
  try {
    const r = await api(
      '/api/workspace/list?path=' + encodeURIComponent(path),
      props.token,
    )
    if (!r.ok) {
      const d = await r.json().catch(() => ({}))
      err.value = d.detail || '无法列出目录'
      return
    }
    const data = await r.json()
    cwd.value = data.path
    parent.value = data.parent
    entries.value = data.entries || []
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function openEntry(e: Entry) {
  if (e.kind === 'dir') {
    selected.value = null
    revokeBlobs()
    previewText.value = ''
    await loadDir(e.path)
    return
  }
  selected.value = e.path
  previewName.value = e.name
  previewText.value = ''
  previewBinary.value = false
  previewTruncated.value = false
  revokeBlobs()

  if (isImage(e.name)) {
    try {
      const r = await fetch(
        '/api/workspace/file?path=' + encodeURIComponent(e.path),
        { headers: { Authorization: 'Bearer ' + props.token } },
      )
      if (!r.ok) {
        err.value = '图片加载失败'
        return
      }
      const blob = await r.blob()
      const url = URL.createObjectURL(blob)
      blobUrls.value.push(url)
      previewImgUrl.value = url
      previewBinary.value = true
    } catch (ex) {
      err.value = ex instanceof Error ? ex.message : String(ex)
    }
    return
  }

  const r = await api(
    '/api/workspace/read?path=' + encodeURIComponent(e.path),
    props.token,
  )
  if (!r.ok) {
    const d = await r.json().catch(() => ({}))
    err.value = d.detail || '读取失败'
    return
  }
  const data = await r.json()
  previewBinary.value = !!data.binary
  previewTruncated.value = !!data.truncated
  previewText.value = data.text || (data.binary ? '(二进制文件，可下载)' : '')
}

watch(
  () => props.active,
  (v) => {
    if (v) loadDir(cwd.value)
  },
)

watch(
  () => props.refreshKey,
  () => {
    if (props.active) loadDir(cwd.value)
  },
)

onMounted(() => {
  if (props.active) loadDir()
})
</script>

<template>
  <div class="ws">
    <div class="ws-toolbar">
      <button
        type="button"
        class="nav"
        :disabled="!parent"
        title="上级目录"
        @click="parent && loadDir(parent)"
      >
        ↑
      </button>
      <button type="button" class="nav" title="刷新" @click="loadDir(cwd)">↻</button>
      <div class="crumbs" :title="cwd">
        <button
          v-for="(c, i) in crumbs"
          :key="c.path"
          type="button"
          class="crumb"
          @click="loadDir(c.path)"
        >
          <span v-if="i">/</span>{{ c.label }}
        </button>
      </div>
    </div>
    <p v-if="err" class="err">{{ err }}</p>
    <div class="ws-body">
      <div class="tree">
        <div v-if="loading" class="empty">加载中…</div>
        <div v-else-if="!entries.length" class="empty">空目录</div>
        <button
          v-for="e in entries"
          :key="e.path"
          type="button"
          class="entry"
          :class="{ active: selected === e.path, dir: e.kind === 'dir' }"
          @click="openEntry(e)"
          @dblclick="e.kind === 'dir' && loadDir(e.path)"
        >
          <span class="ico">{{ e.kind === 'dir' ? '📁' : isImage(e.name) ? '🖼' : '📄' }}</span>
          <span class="name">{{ e.name }}</span>
          <span v-if="e.kind === 'file'" class="size">{{ fmtSize(e.size) }}</span>
        </button>
      </div>
      <div class="preview">
        <div v-if="!selected" class="empty preview-empty">
          选择文件预览<br />
          <small>对应沙箱 <code>/workspace</code></small>
        </div>
        <template v-else>
          <div class="preview-head">
            <strong>{{ previewName }}</strong>
            <span class="path">{{ selected }}</span>
          </div>
          <img v-if="previewImgUrl" :src="previewImgUrl" class="preview-img" alt="preview" />
          <pre v-else-if="previewText" class="preview-code">{{ previewText }}</pre>
          <div v-else-if="previewBinary" class="empty">二进制文件</div>
          <div v-if="previewTruncated" class="trunc">已截断预览（前 512KB）</div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ws {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}
.ws-toolbar {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 8px;
  border-bottom: 1px solid var(--line);
}
.nav {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  color: var(--dim);
  font-size: 14px;
}
.nav:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--fg);
}
.nav:disabled {
  opacity: 0.35;
}
.crumbs {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  font-size: 11.5px;
  font-family: var(--mono);
  color: var(--muted);
}
.crumb {
  color: var(--dim);
  font-family: var(--mono);
  font-size: 11.5px;
  padding: 0 1px;
}
.crumb:hover {
  color: var(--blue);
}
.err {
  margin: 0;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--err);
}
.ws-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.tree {
  flex: 0 0 42%;
  overflow-y: auto;
  border-bottom: 1px solid var(--line);
  padding: 4px 6px 8px;
}
.entry {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 6px;
  text-align: left;
  padding: 6px 8px;
  border-radius: 8px;
  font-size: 12.5px;
  color: var(--fg-2);
}
.entry:hover {
  background: var(--bg-hover);
}
.entry.active {
  background: var(--bg-3);
}
.entry .ico {
  flex-shrink: 0;
  font-size: 13px;
}
.entry .name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.entry .size {
  font-size: 10px;
  color: var(--muted);
  font-family: var(--mono);
}
.empty {
  padding: 16px 10px;
  color: var(--muted);
  font-size: 12.5px;
  line-height: 1.45;
  text-align: center;
}
.preview {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 8px;
}
.preview-empty small {
  color: var(--muted);
}
.preview-empty code {
  font-family: var(--mono);
  font-size: 11px;
}
.preview-head {
  margin-bottom: 8px;
}
.preview-head strong {
  display: block;
  font-size: 13px;
}
.preview-head .path {
  font-size: 11px;
  font-family: var(--mono);
  color: var(--muted);
  word-break: break-all;
}
.preview-code {
  margin: 0;
  padding: 10px;
  background: var(--pre-bg);
  border: 1px solid var(--line);
  border-radius: 10px;
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--fg-2);
}
.preview-img {
  max-width: 100%;
  height: auto;
  border-radius: 10px;
  border: 1px solid var(--line);
}
.trunc {
  margin-top: 6px;
  font-size: 11px;
  color: var(--warn);
}
</style>

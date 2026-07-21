<script setup lang="ts">
/**
 * KNOWLEDGE.md panel — Codex AGENTS.md style: user-owned project brief.
 * View + full edit only. No structured auto-ops; Memory handles recall.
 */
import { computed, ref, watch } from 'vue'
import { api } from '../api/client'
import MarkdownView from './MarkdownView.vue'

const props = defineProps<{
  token: string
  atelierId: string
  open: boolean
}>()

const emit = defineEmits<{
  close: []
  updated: []
}>()

const content = ref('')
const loading = ref(false)
const saving = ref(false)
const err = ref('')
const mode = ref<'view' | 'edit'>('view')
const draft = ref('')

const dirty = computed(() => mode.value === 'edit' && draft.value !== content.value)

async function load() {
  if (!props.token || !props.atelierId) return
  loading.value = true
  err.value = ''
  try {
    const r = await api(
      '/api/ateliers/' + encodeURIComponent(props.atelierId) + '/knowledge',
      props.token,
    )
    if (!r.ok) {
      const d = await r.json().catch(() => ({}))
      err.value = d.detail || '加载失败'
      return
    }
    const data = await r.json()
    content.value = data.content || ''
    draft.value = content.value
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function saveFull() {
  saving.value = true
  err.value = ''
  try {
    const r = await api(
      '/api/ateliers/' + encodeURIComponent(props.atelierId) + '/knowledge',
      props.token,
      { method: 'PUT', body: JSON.stringify({ content: draft.value }) },
    )
    if (!r.ok) {
      const d = await r.json().catch(() => ({}))
      err.value = d.detail || '保存失败'
      return
    }
    content.value = draft.value
    mode.value = 'view'
    emit('updated')
  } finally {
    saving.value = false
  }
}

function startEdit() {
  draft.value = content.value
  mode.value = 'edit'
}

function cancelEdit() {
  draft.value = content.value
  mode.value = 'view'
}

watch(
  () => [props.open, props.atelierId] as const,
  ([open]) => {
    if (open) {
      mode.value = 'view'
      load()
    }
  },
  { immediate: true },
)
</script>

<template>
  <aside class="know" :class="{ open }">
    <header class="know-head">
      <div class="titles">
        <span class="k-label">项目说明</span>
        <span class="k-sub">KNOWLEDGE.md · 用户维护</span>
      </div>
      <div class="actions">
        <button
          v-if="mode === 'view'"
          type="button"
          class="chip-btn"
          title="编辑"
          @click="startEdit"
        >✎</button>
        <button type="button" class="chip-btn" title="关闭" @click="emit('close')">×</button>
      </div>
    </header>

    <p class="hint">
      类似 Codex <code>AGENTS.md</code>：写稳定决策与约定，跨会话始终注入。
      自动记忆请用 Memory，勿指望自动提取。
    </p>

    <p v-if="err" class="err">{{ err }}</p>
    <p v-else-if="loading" class="muted">加载中…</p>

    <template v-else>
      <div v-if="mode === 'view'" class="know-body">
        <MarkdownView :source="content || '_(空 — 点 ✎ 写几条决策)_'" />
      </div>
      <div v-else class="know-edit">
        <textarea
          v-model="draft"
          spellcheck="false"
          rows="18"
          placeholder="# 项目名&#10;&#10;## 决策与约定&#10;- 认证: JWT&#10;- 风格: ruff"
        />
        <div class="edit-bar">
          <span v-if="dirty" class="dirty">未保存</span>
          <button type="button" class="secondary" @click="cancelEdit">取消</button>
          <button type="button" class="primary" :disabled="saving" @click="saveFull">保存</button>
        </div>
      </div>
    </template>
  </aside>
</template>

<style scoped>
.know {
  width: 0;
  opacity: 0;
  overflow: hidden;
  border-left: 1px solid var(--line);
  background: var(--bg-2);
  display: flex;
  flex-direction: column;
  transition: width 0.2s, opacity 0.2s;
  flex-shrink: 0;
}
.know.open {
  width: min(360px, 34vw);
  opacity: 1;
}
.know-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 12px 8px;
  border-bottom: 1px solid var(--line);
  gap: 8px;
}
.titles { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.k-label { font-weight: 700; font-size: 13px; }
.k-sub { font-size: 11px; color: var(--muted); font-family: var(--mono); }
.actions { display: flex; gap: 4px; }
.chip-btn {
  width: 30px; height: 30px; border-radius: 8px;
  background: var(--bg-3); border: 1px solid var(--line);
  color: var(--fg-2); font-size: 14px;
}
.chip-btn:hover { background: var(--bg-hover); }
.hint {
  margin: 0;
  padding: 10px 14px;
  font-size: 12px;
  line-height: 1.45;
  color: var(--dim);
  border-bottom: 1px solid var(--line);
  background: color-mix(in srgb, var(--blue) 6%, transparent);
}
.hint code {
  font-family: var(--mono);
  font-size: 11px;
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--code-bg);
}
.know-body, .know-edit {
  flex: 1;
  overflow: auto;
  padding: 12px 14px 20px;
}
.know-edit textarea {
  width: 100%;
  min-height: 280px;
  background: var(--bg-3);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 10px 12px;
  resize: vertical;
  font-family: var(--mono);
  font-size: 12.5px;
  line-height: 1.5;
}
.edit-bar {
  display: flex;
  gap: 8px;
  align-items: center;
  justify-content: flex-end;
  margin-top: 10px;
}
.edit-bar .primary {
  background: var(--btn);
  color: var(--btn-fg);
  padding: 8px 14px;
  border-radius: 10px;
  font-weight: 600;
  font-size: 13px;
}
.edit-bar .secondary {
  padding: 8px 12px;
  border-radius: 10px;
  border: 1px solid var(--line);
  color: var(--dim);
  font-size: 13px;
}
.dirty { margin-right: auto; font-size: 12px; color: var(--warn); }
.err { color: var(--err); padding: 12px; font-size: 13px; }
.muted { color: var(--muted); padding: 12px; font-size: 13px; }
</style>

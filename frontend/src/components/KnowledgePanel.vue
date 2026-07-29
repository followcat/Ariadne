<script setup lang="ts">
/**
 * 本坊便签：只属于当前作坊。主线可手写；主线对话里明确约定时也会自动补几条。
 */
import { computed, ref, watch } from 'vue'
import { api } from '../api/client'
import MarkdownView from './MarkdownView.vue'

const props = defineProps<{
  token: string
  atelierId: string
  /** 展示名，如「画画」 */
  atelierName?: string
  /** main | branch-… — 旁支时本本只读（权威在主线） */
  atelierSession?: string
  open: boolean
  /** Bump after main turns so open panel reloads auto-updated brief */
  refreshKey?: number
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
const isBranch = computed(() => {
  const s = (props.atelierSession || 'main').trim()
  return s !== 'main' && s.startsWith('branch-')
})
const workshopLabel = computed(
  () => (props.atelierName || props.atelierId || '当前作坊').trim(),
)
const canEdit = computed(() => !isBranch.value)
const editPlaceholder = computed(
  () =>
    `# ${workshopLabel.value}\n\n## 本坊怎么运作\n- 目标与流程…\n\n## 关键路径\n- /workspace → …\n\n## 注意\n- …`,
)

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
      err.value = d.detail || '打不开小本本'
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
  if (!canEdit.value) return
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
      err.value = d.detail || '没存上'
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
  if (!canEdit.value) return
  draft.value = content.value
  mode.value = 'edit'
}

function cancelEdit() {
  draft.value = content.value
  mode.value = 'view'
}

watch(
  () => [props.open, props.atelierId, props.refreshKey] as const,
  ([open]) => {
    if (open) {
      mode.value = 'view'
      load()
    }
  },
  { immediate: true },
)

watch(
  () => props.atelierSession,
  () => {
    // 切主线/旁支：旁支不可编辑，退出编辑态
    if (isBranch.value) mode.value = 'view'
  },
)
</script>

<template>
  <aside class="know" :class="{ open }">
    <header class="know-head">
      <div class="titles">
        <span class="k-label">本坊小本本</span>
        <span class="k-sub">仅「{{ workshopLabel }}」· 不是全局</span>
      </div>
      <div class="actions">
        <button
          v-if="mode === 'view' && canEdit"
          type="button"
          class="chip-btn"
          title="改这一坊的便签"
          @click="startEdit"
        >✎</button>
        <button type="button" class="chip-btn" title="合上" @click="emit('close')">×</button>
      </div>
    </header>

    <div class="scope-bar">
      <span class="scope-pill">◈ {{ workshopLabel }}</span>
      <span class="scope-pill soft">{{ isBranch ? '旁支只读' : '主线可改' }}</span>
    </div>

    <p class="hint">
      记<strong>这间作坊怎么运作</strong>：关键路径、怎么跑、注意点（不是聊天日记）。
      换作坊是另一本。权威在作坊根 <code>KNOWLEDGE.md</code>，不在沙箱
      <code>/workspace</code> 里。
      <strong>主线</strong>聊清运作约定时会<strong>自动补几条</strong>；旁支只读。
      <template v-if="isBranch">
        <br />旁支只读；要改请回<strong>主线</strong>。
      </template>
    </p>

    <p v-if="err" class="err">{{ err }}</p>
    <p v-else-if="loading" class="muted">翻本本…</p>

    <template v-else>
      <div v-if="mode === 'view'" class="know-body">
        <MarkdownView
          :source="
            content ||
            '_(本坊便签还是空的。' +
            (canEdit ? '点 ✎ 写两笔，只对本坊生效。' : '回主线才能改。') +
            ')_'
          "
        />
      </div>
      <div v-else class="know-edit">
        <p class="edit-scope">正在编辑 · {{ workshopLabel }} 的便签</p>
        <textarea
          v-model="draft"
          spellcheck="false"
          rows="18"
          :placeholder="editPlaceholder"
        />
        <div class="edit-bar">
          <span v-if="dirty" class="dirty">还没存</span>
          <button type="button" class="secondary" @click="cancelEdit">算了</button>
          <button type="button" class="primary" :disabled="saving" @click="saveFull">
            存到本坊
          </button>
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
.k-sub {
  font-size: 11px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.actions { display: flex; gap: 4px; flex-shrink: 0; }
.chip-btn {
  width: 30px; height: 30px; border-radius: 8px;
  background: var(--bg-3); border: 1px solid var(--line);
  color: var(--fg-2); font-size: 14px;
}
.chip-btn:hover { background: var(--bg-hover); }
.scope-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 8px 12px 0;
}
.scope-pill {
  font-size: 11.5px;
  font-weight: 600;
  padding: 3px 8px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--blue) 14%, var(--bg-3));
  border: 1px solid color-mix(in srgb, var(--blue) 35%, var(--line));
  color: var(--fg-2);
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.scope-pill.soft {
  font-weight: 500;
  color: var(--dim);
  background: var(--bg-3);
  border-color: var(--line);
}
.hint {
  margin: 0;
  padding: 10px 14px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--dim);
  border-bottom: 1px solid var(--line);
  background: color-mix(in srgb, var(--blue) 6%, transparent);
}
.hint strong { color: var(--fg-2); font-weight: 650; }
.edit-scope {
  margin: 0 0 8px;
  font-size: 12px;
  font-weight: 600;
  color: var(--blue);
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

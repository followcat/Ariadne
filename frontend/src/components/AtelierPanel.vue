<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../api/client'

export type AtelierRow = {
  id: string
  name: string
  workspace_path: string
  path: string
  created_at: number
  updated_at: number
}

export type AtelierSession = {
  id: string
  title: string
  type: string
  status: string
  branch_name?: string | null
  parent_session_id?: string | null
  created_at: number
  updated_at: number
}

const props = defineProps<{
  token: string
  active: boolean
  selectedId: string
  selectedSession: string
}>()

const emit = defineEmits<{
  select: [id: string, name?: string]
  selectSession: [sessionId: string]
  exit: []
  openKnowledge: []
}>()

const list = ref<AtelierRow[]>([])
const sessions = ref<AtelierSession[]>([])
const loading = ref(false)
const err = ref('')
const showCreate = ref(false)
const newName = ref('')
const creating = ref(false)
const branchName = ref('')
const creatingBranch = ref(false)

const selected = computed(() => list.value.find((a) => a.id === props.selectedId) || null)

const activeSessions = computed(() =>
  sessions.value.filter((s) => s.status === 'active' || s.id === 'main'),
)
const closedSessions = computed(() =>
  sessions.value.filter((s) => s.status !== 'active' && s.id !== 'main'),
)

/** Human status line — never stamp every row with the same 进行中. */
function formatRelative(ts: number): string {
  if (!ts || !Number.isFinite(ts)) return ''
  // Server may send seconds or ms
  const sec = ts > 1e12 ? ts / 1000 : ts
  const diff = Date.now() / 1000 - sec
  if (diff < 45) return '刚刚'
  if (diff < 3600) return Math.max(1, Math.floor(diff / 60)) + ' 分钟前'
  if (diff < 86400) return Math.max(1, Math.floor(diff / 3600)) + ' 小时前'
  if (diff < 86400 * 7) return Math.max(1, Math.floor(diff / 86400)) + ' 天前'
  const d = new Date(sec * 1000)
  return d.getMonth() + 1 + '/' + d.getDate()
}

function sessionMetaLine(s: AtelierSession): string {
  if (s.type === 'main' || s.id === 'main') return '本坊主线'
  if (s.status === 'merged') return '已收'
  if (s.status === 'discarded') return '已丢'
  if (s.status === 'active') {
    const rel = formatRelative(s.updated_at)
    return rel ? '最近 ' + rel : '旁支'
  }
  return s.status || '旁支'
}

async function loadList() {
  if (!props.token) return
  loading.value = true
  err.value = ''
  try {
    const r = await api('/api/ateliers', props.token)
    if (!r.ok) {
      const d = await r.json().catch(() => ({}))
      err.value = d.detail || '加载失败，稍后再试'
      return
    }
    list.value = await r.json()
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function loadSessions() {
  if (!props.token || !props.selectedId) {
    sessions.value = []
    return
  }
  const r = await api(
    '/api/ateliers/' + encodeURIComponent(props.selectedId) + '/sessions',
    props.token,
  )
  if (!r.ok) return
  sessions.value = await r.json()
}

async function createAtelier() {
  const name = newName.value.trim()
  if (!name) return
  creating.value = true
  err.value = ''
  try {
    const r = await api('/api/ateliers', props.token, {
      method: 'POST',
      body: JSON.stringify({ name, no_scan: false }),
    })
    const d = await r.json().catch(() => ({}))
    if (!r.ok) {
      err.value = d.detail || '创建失败'
      return
    }
    showCreate.value = false
    newName.value = ''
    await loadList()
    emit('select', d.id)
    emit('selectSession', 'main')
  } finally {
    creating.value = false
  }
}

async function removeAtelier(id: string) {
  if (!confirm(`删掉「${id}」？删了就没了哦。`)) return
  const r = await api(
    '/api/ateliers/' + encodeURIComponent(id) + '?yes=true',
    props.token,
    { method: 'DELETE' },
  )
  if (!r.ok) {
    const d = await r.json().catch(() => ({}))
    err.value = d.detail || '删除失败'
    return
  }
  if (props.selectedId === id) emit('exit')
  await loadList()
}

async function createBranch() {
  const name = branchName.value.trim()
  if (!name || !props.selectedId) return
  creatingBranch.value = true
  try {
    const r = await api(
      '/api/ateliers/' + encodeURIComponent(props.selectedId) + '/branches',
      props.token,
      { method: 'POST', body: JSON.stringify({ name }) },
    )
    const d = await r.json().catch(() => ({}))
    if (!r.ok) {
      err.value = d.detail || '创建分支失败'
      return
    }
    branchName.value = ''
    await loadSessions()
    emit('selectSession', d.id)
  } finally {
    creatingBranch.value = false
  }
}

async function mergeBranch(branch: string) {
  if (!props.selectedId) return
  if (
    !confirm(
      `收起旁支「${branch}」？只归档旁支摘要，不会改主线文件，也不会改本坊便签。`,
    )
  )
    return
  const r = await api(
    `/api/ateliers/${encodeURIComponent(props.selectedId)}/branches/${encodeURIComponent(branch)}/merge`,
    props.token,
    { method: 'POST', body: '{}' },
  )
  const d = await r.json().catch(() => ({}))
  if (!r.ok) {
    err.value = d.detail || '合并失败'
    return
  }
  await loadSessions()
  emit('selectSession', 'main')
  emit('openKnowledge')
}

async function discardBranch(branch: string) {
  if (!props.selectedId) return
  if (!confirm(`丢掉旁支「${branch}」？聊天不要了，文件还在。`)) return
  const r = await api(
    `/api/ateliers/${encodeURIComponent(props.selectedId)}/branches/${encodeURIComponent(branch)}/discard`,
    props.token,
    { method: 'POST', body: '{}' },
  )
  if (!r.ok) {
    const d = await r.json().catch(() => ({}))
    err.value = d.detail || '丢弃失败'
    return
  }
  await loadSessions()
  if (props.selectedSession.includes(branch)) emit('selectSession', 'main')
}

function pick(id: string) {
  const row = list.value.find((a) => a.id === id)
  emit('select', id, row?.name)
  emit('selectSession', 'main')
}

watch(
  () => [props.active, props.token] as const,
  ([active]) => {
    if (active) loadList()
  },
  { immediate: true },
)

watch(
  () => props.selectedId,
  () => loadSessions(),
  { immediate: true },
)

defineExpose({ reload: loadList, reloadSessions: loadSessions })
</script>

<template>
  <div class="atelier-panel">
    <!-- List mode -->
    <template v-if="!selectedId">
      <div class="ap-head">
        <div>
          <div class="ap-title">小作坊</div>
          <div class="ap-sub">一个小角落 · 随便折腾</div>
        </div>
        <button type="button" class="new-btn" @click="showCreate = !showCreate">
          {{ showCreate ? '取消' : '+ 开一个' }}
        </button>
      </div>

      <div v-if="showCreate" class="create-box">
        <input
          v-model="newName"
          placeholder="起个名字，比如 画画"
          maxlength="64"
          @keydown.enter.prevent="createAtelier"
        />
        <button type="button" class="primary" :disabled="creating || !newName.trim()" @click="createAtelier">
          开干
        </button>
      </div>
      <p v-if="showCreate" class="create-hint">中文名也行，想叫啥叫啥</p>

      <p v-if="err" class="err">{{ err }}</p>
      <p v-else-if="loading" class="muted">加载中…</p>
      <div v-else-if="!list.length" class="empty">
        <div class="empty-ico">◈</div>
        <p>还空着</p>
        <p class="hint">开一个小角落，代码和聊天都放这儿</p>
      </div>
      <div v-else class="alist">
        <button
          v-for="a in list"
          :key="a.id"
          type="button"
          class="arow"
          @click="pick(a.id)"
        >
          <span class="mark">◈</span>
          <span class="body">
            <span class="name">{{ a.name }}</span>
            <span class="path">{{ a.id }}</span>
          </span>
          <span class="del" title="删除" @click.stop="removeAtelier(a.id)">×</span>
        </button>
      </div>
    </template>

    <!-- Opened atelier -->
    <template v-else>
      <div class="ap-head open">
        <button type="button" class="back" @click="emit('exit')" title="返回列表">←</button>
        <div class="flex-1">
          <div class="ap-title">{{ selected?.name || selectedId }}</div>
          <div class="ap-sub mono">{{ selectedId }}</div>
        </div>
      </div>

      <p v-if="err" class="err">{{ err }}</p>

      <button
        type="button"
        class="knowledge-entry"
        title="只属于当前这一间作坊的运作说明"
        @click="emit('openKnowledge')"
      >
        <span class="ke-ico">◈</span>
        <span class="ke-body">
          <span class="ke-title">本坊便签</span>
          <span class="ke-sub">仅「{{ selected?.name || selectedId }}」· 换作坊不会带走</span>
        </span>
        <span class="ke-go">打开</span>
      </button>

      <p class="scope-note">
        主线文件也<strong>只属于本坊</strong>；旁支动手不写回主线。
      </p>

      <div class="sec-label">聊到哪儿了</div>
      <div class="slist">
        <button
          v-for="s in activeSessions"
          :key="s.id"
          type="button"
          class="srow"
          :class="{ on: s.id === selectedSession || (selectedSession === 'main' && s.id === 'main') }"
          @click="emit('selectSession', s.id)"
        >
          <span class="badge" :class="s.type">{{ s.type === 'main' ? '主' : '玩' }}</span>
          <span class="body">
            <span class="name">{{ s.type === 'main' ? '主线' : (s.title || s.id) }}</span>
            <span class="meta">{{ sessionMetaLine(s) }}</span>
          </span>
          <template v-if="s.type === 'branch' && s.status === 'active' && s.branch_name">
            <span
              class="mini ok"
              title="收进主线"
              @click.stop="mergeBranch(s.branch_name!)"
            >收</span>
            <span
              class="mini warn"
              title="丢掉"
              @click.stop="discardBranch(s.branch_name!)"
            >丢</span>
          </template>
        </button>
      </div>

      <div class="branch-create">
        <input
          v-model="branchName"
          placeholder="旁支名，中文也行，如 V字仇杀队"
          @keydown.enter.prevent="createBranch"
        />
        <button type="button" class="primary sm" :disabled="creatingBranch || !branchName.trim()" @click="createBranch">
          开旁支
        </button>
      </div>
      <p class="create-hint" style="margin-top: 0">中文名会自动生成内部 id，界面仍显示你起的名字</p>

      <div v-if="closedSessions.length" class="sec-label dim">以前的</div>
      <div v-if="closedSessions.length" class="slist closed">
        <div v-for="s in closedSessions" :key="s.id" class="srow closed">
          <span class="badge">{{ s.status === 'merged' ? '已收' : s.status === 'discarded' ? '已丢' : s.status }}</span>
          <span class="body">
            <span class="name">{{ s.title || s.id }}</span>
          </span>
        </div>
      </div>

      <div class="tips">
        <p>· <b>本坊便签</b>：本坊怎么运作 / 路径 / 注意；主线可自动补约定，旁支只读</p>
        <p>· <b>主线</b>：定方向；文件在 <code>/workspace</code></p>
        <p>· <b>旁支</b>：可写 <code>/workspace</code>；读主线最新用 <code>/main-readonly</code>；「收」不写回主线</p>
      </div>
    </template>
  </div>
</template>

<style scoped>
.atelier-panel {
  flex: 1;
  min-height: 0;
  overflow: auto;
  display: flex;
  flex-direction: column;
  padding-bottom: 12px;
}
.ap-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 12px 8px;
}
.ap-head.open { align-items: center; }
.ap-title { font-weight: 700; font-size: 13.5px; }
.ap-sub { font-size: 11.5px; color: var(--muted); margin-top: 2px; }
.ap-sub.mono, .mono { font-family: var(--mono); }
.knowledge-entry {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 12px 8px;
  padding: 10px 12px;
  width: calc(100% - 24px);
  text-align: left;
  border-radius: 12px;
  border: 1px solid color-mix(in srgb, var(--blue) 40%, var(--line));
  background: color-mix(in srgb, var(--blue) 10%, var(--bg-3));
  cursor: pointer;
}
.knowledge-entry:hover {
  background: color-mix(in srgb, var(--blue) 16%, var(--bg-hover));
}
.ke-ico {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  border-radius: 8px;
  display: grid;
  place-items: center;
  background: color-mix(in srgb, var(--blue) 22%, transparent);
  color: var(--blue);
  font-size: 14px;
}
.ke-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ke-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--fg);
}
.ke-sub {
  font-size: 11px;
  color: var(--muted);
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ke-go {
  flex-shrink: 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--blue);
}
.scope-note {
  margin: 0 12px 8px;
  padding: 8px 10px;
  font-size: 11.5px;
  line-height: 1.45;
  color: var(--dim);
  background: color-mix(in srgb, var(--blue) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--blue) 25%, var(--line));
  border-radius: 10px;
}
.scope-note strong { color: var(--fg-2); font-weight: 650; }
.flex-1 { flex: 1; min-width: 0; }
.back {
  width: 30px; height: 30px; border-radius: 8px;
  border: 1px solid var(--line); background: var(--bg-3);
  font-size: 14px;
}
.back:hover { background: var(--bg-hover); }
.new-btn, .chip {
  padding: 6px 10px;
  border-radius: 9px;
  border: 1px solid var(--line);
  background: var(--bg-3);
  font-size: 12px;
  font-weight: 600;
  color: var(--fg-2);
  white-space: nowrap;
}
.new-btn:hover, .chip:hover { background: var(--bg-hover); }
.chip { color: var(--blue); border-color: color-mix(in srgb, var(--blue) 35%, var(--line)); }
.create-box {
  display: flex;
  gap: 6px;
  padding: 0 12px 10px;
}
.create-box input, .branch-create input {
  flex: 1;
  min-width: 0;
  background: var(--bg-3);
  border: 1px solid var(--line);
  border-radius: 9px;
  padding: 8px 10px;
  font-size: 13px;
}
.primary {
  background: var(--btn);
  color: var(--btn-fg);
  border-radius: 9px;
  padding: 8px 12px;
  font-weight: 600;
  font-size: 13px;
}
.primary.sm { padding: 7px 10px; font-size: 12px; }
.alist, .slist { display: flex; flex-direction: column; gap: 2px; padding: 0 8px; }
.arow, .srow {
  display: flex;
  align-items: center;
  gap: 8px;
  text-align: left;
  padding: 9px 8px;
  border-radius: 10px;
  width: 100%;
  color: var(--fg-2);
}
.arow:hover, .srow:hover { background: var(--bg-hover); }
.srow.on {
  background: color-mix(in srgb, var(--blue) 14%, transparent);
  outline: 1px solid color-mix(in srgb, var(--blue) 30%, transparent);
}
.arow .mark {
  width: 28px; height: 28px; border-radius: 8px;
  background: linear-gradient(135deg, #1d9bf0 0%, #7856ff 100%);
  color: #fff; display: grid; place-items: center; font-size: 12px; flex-shrink: 0;
}
.arow .body, .srow .body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.arow .name, .srow .name {
  font-size: 13px; font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.arow .path, .srow .meta { font-size: 11px; color: var(--muted); }
.del {
  opacity: 0; width: 22px; height: 22px; border-radius: 6px;
  display: grid; place-items: center; color: var(--muted); font-size: 16px;
}
.arow:hover .del { opacity: 1; }
.del:hover { background: color-mix(in srgb, var(--err) 20%, transparent); color: var(--err); }
.badge {
  font-size: 10px; font-weight: 700;
  padding: 2px 6px; border-radius: 6px;
  background: var(--bg-3); border: 1px solid var(--line); color: var(--dim);
  flex-shrink: 0;
}
.badge.main { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 40%, var(--line)); }
.badge.branch { color: var(--blue); border-color: color-mix(in srgb, var(--blue) 40%, var(--line)); }
.mini {
  font-size: 11px; font-weight: 700;
  padding: 2px 6px; border-radius: 6px;
  border: 1px solid var(--line); color: var(--dim);
}
.mini.ok:hover { color: var(--ok); border-color: var(--ok); }
.mini.warn:hover { color: var(--warn); border-color: var(--warn); }
.sec-label {
  font-size: 11px; font-weight: 700; color: var(--dim);
  text-transform: uppercase; letter-spacing: .04em;
  padding: 12px 14px 4px;
}
.sec-label.dim { color: var(--muted); }
.branch-create {
  display: flex; gap: 6px; padding: 10px 12px;
}
.slist.closed .srow { opacity: .55; }
.tips {
  margin-top: auto;
  padding: 12px 14px;
  font-size: 11.5px;
  color: var(--muted);
  line-height: 1.5;
  border-top: 1px solid var(--line);
}
.tips p { margin: 0 0 4px; }
.empty {
  text-align: center; padding: 36px 16px; color: var(--dim);
}
.empty-ico {
  width: 48px; height: 48px; margin: 0 auto 12px;
  border-radius: 14px;
  background: linear-gradient(135deg, #1d9bf0 0%, #7856ff 100%);
  color: #fff; display: grid; place-items: center; font-size: 20px;
}
.empty p { margin: 0 0 6px; font-weight: 600; }
.empty .hint { font-weight: 400; font-size: 12.5px; color: var(--muted); }
.err { color: var(--err); font-size: 12.5px; padding: 8px 12px; }
.muted { color: var(--muted); font-size: 12.5px; padding: 8px 12px; }
.create-hint {
  margin: -4px 12px 8px;
  font-size: 11.5px;
  color: var(--muted);
  line-height: 1.4;
}
</style>

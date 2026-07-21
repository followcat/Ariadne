<script setup lang="ts">
import { computed, nextTick, onMounted, provide, ref, watch } from 'vue'
import AuthView from './components/AuthView.vue'
import MarkdownView from './components/MarkdownView.vue'
import PluginsModal from './components/PluginsModal.vue'
import ThinkingBlock from './components/ThinkingBlock.vue'
import ToolsPanel, { type ToolEntry } from './components/ToolsPanel.vue'
import WorkspaceBrowser from './components/WorkspaceBrowser.vue'
import { api, parseSseBuffer, type Me, type SessionRow, type StreamEvent } from './api/client'
import { setHostWorkspaceRoot } from './lib/markdown'

type LeftTab = 'sessions' | 'workspace'

type ChatMsg =
  | { id: string; role: 'user'; content: string }
  | {
      id: string
      role: 'assistant'
      content: string
      thinking: string
      streaming: boolean
      thinkingLive: boolean
    }

const token = ref(localStorage.getItem('ariadne_token') || '')
provide('authToken', token)
const username = ref('')
const me = ref<Me | null>(null)
const sessionId = ref(localStorage.getItem('ariadne_session') || '')
const sessions = ref<SessionRow[]>([])
const messages = ref<ChatMsg[]>([])
const input = ref('')
const busy = ref(false)
const sidebarCollapsed = ref(localStorage.getItem('ariadne_sidebar') === '0')
const toolsCollapsed = ref(localStorage.getItem('ariadne_tools_panel') !== '1')
const leftTab = ref<LeftTab>(
  localStorage.getItem('ariadne_left_tab') === 'workspace' ? 'workspace' : 'sessions',
)
/** Bumped after turns so workspace browser reloads new agent outputs. */
const workspaceRefreshKey = ref(0)
const theme = ref(document.documentElement.getAttribute('data-theme') || 'dark')

const tools = ref<ToolEntry[]>([])
const toolsOpenIds = ref(new Set<string>())
const chatEl = ref<HTMLElement | null>(null)
const inputEl = ref<HTMLTextAreaElement | null>(null)
/** Wall-clock start of the in-flight turn (ms). */
let turnStartedAt = 0

const authed = computed(() => !!token.value)
const sessionLabel = computed(() => {
  const id = sessionId.value
  if (!id) return '—'
  return id.length > 20 ? id.slice(0, 10) + '…' + id.slice(-4) : id
})
const providerOk = computed(() => !!me.value?.provider_configured)
const topTitle = computed(() => {
  const s = sessions.value.find((x) => x.session_id === sessionId.value)
  return s?.title || 'Ariadne'
})

function setTheme(t: string) {
  theme.value = t === 'light' ? 'light' : 'dark'
  document.documentElement.setAttribute('data-theme', theme.value)
  localStorage.setItem('ariadne_theme', theme.value)
}
function toggleTheme() {
  setTheme(theme.value === 'light' ? 'dark' : 'light')
}
function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value
  localStorage.setItem('ariadne_sidebar', sidebarCollapsed.value ? '0' : '1')
}
function setLeftTab(tab: LeftTab) {
  leftTab.value = tab
  localStorage.setItem('ariadne_left_tab', tab)
  // Opening workspace while sidebar is collapsed expands it (Codex-style browse).
  if (tab === 'workspace' && sidebarCollapsed.value) {
    sidebarCollapsed.value = false
    localStorage.setItem('ariadne_sidebar', '1')
  }
}
function toggleTools() {
  toolsCollapsed.value = !toolsCollapsed.value
  localStorage.setItem('ariadne_tools_panel', toolsCollapsed.value ? '0' : '1')
}

function onAuthed(t: string) {
  token.value = t
  localStorage.setItem('ariadne_token', t)
  bootstrap()
}
function logout() {
  token.value = ''
  username.value = ''
  me.value = null
  localStorage.removeItem('ariadne_token')
}

async function bootstrap() {
  try {
    const r = await api('/api/me', token.value)
    me.value = await r.json()
    username.value = me.value?.username || ''
    // So chat can rewrite host absolute paths like /home/…/plot.png → workspace images
    setHostWorkspaceRoot(me.value?.workspace || '')
    if (!sessionId.value) {
      const sr = await api('/api/sessions', token.value, { method: 'POST' })
      if (sr.ok) {
        const data = await sr.json()
        setSession(data.session_id)
      }
    }
    await Promise.all([loadSessions(), loadHistory()])
  } catch (e) {
    if ((e as { status?: number }).status === 401) logout()
  }
}

function setSession(id: string) {
  sessionId.value = id
  localStorage.setItem('ariadne_session', id)
}

async function loadSessions() {
  const r = await api('/api/sessions', token.value)
  if (!r.ok) return
  sessions.value = await r.json()
}

/** Auto topic title after turns (same as legacy web UI PATCH refresh_title). */
async function refreshSessionTitle(force = false) {
  if (!sessionId.value) return
  try {
    const r = await api(
      '/api/sessions/' + encodeURIComponent(sessionId.value),
      token.value,
      {
        method: 'PATCH',
        body: JSON.stringify({ refresh_title: true, force: !!force }),
      },
    )
    if (!r.ok) return
    const data = await r.json()
    // Update sidebar + topbar from server even when skipped (keeps title in sync).
    if (data.title) {
      const sid = sessionId.value
      const row = sessions.value.find((s) => s.session_id === sid)
      if (row) row.title = data.title
      else await loadSessions()
    } else {
      await loadSessions()
    }
  } catch {
    /* non-fatal */
  }
}

async function loadHistory() {
  if (!sessionId.value) {
    messages.value = []
    return
  }
  const r = await api('/api/sessions/' + encodeURIComponent(sessionId.value), token.value)
  if (!r.ok) {
    messages.value = []
    return
  }
  const data = await r.json()
  messages.value = (data.messages || [])
    .filter((m: { role: string }) => m.role === 'user' || m.role === 'assistant')
    .map((m: { role: string; content: string }, i: number) =>
      m.role === 'user'
        ? { id: 'h-u-' + i, role: 'user' as const, content: m.content || '' }
        : {
            id: 'h-a-' + i,
            role: 'assistant' as const,
            content: m.content || '',
            thinking: '',
            streaming: false,
            thinkingLive: false,
          },
    )
  await scrollChat()
}

async function createSession() {
  const r = await api('/api/sessions', token.value, { method: 'POST' })
  if (!r.ok) return
  const data = await r.json()
  setSession(data.session_id)
  messages.value = []
  tools.value = []
  await loadSessions()
}

async function selectSession(id: string) {
  setSession(id)
  tools.value = []
  await loadHistory()
}

async function deleteSession(id: string) {
  await api('/api/sessions/' + encodeURIComponent(id), token.value, { method: 'DELETE' })
  if (sessionId.value === id) {
    sessionId.value = ''
    localStorage.removeItem('ariadne_session')
    messages.value = []
  }
  await loadSessions()
  if (!sessionId.value && sessions.value[0]) {
    await selectSession(sessions.value[0].session_id)
  }
}

function fmtTime(ts: number) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return d.getMonth() + 1 + '/' + d.getDate() + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes())
}

async function scrollChat() {
  await nextTick()
  const el = chatEl.value
  if (!el) return
  // Pin the conversation to the latest assistant / thinking output.
  el.scrollTop = el.scrollHeight
  // Second frame: thinking-body max-height layout may grow after paint.
  requestAnimationFrame(() => {
    if (chatEl.value) chatEl.value.scrollTop = chatEl.value.scrollHeight
  })
}

function upsertTool(entry: Partial<ToolEntry> & { call_id: string; name: string }) {
  const idx = tools.value.findIndex((t) => t.call_id === entry.call_id)
  const next: ToolEntry = {
    call_id: entry.call_id,
    name: entry.name,
    status: entry.status || 'started',
    arguments: entry.arguments,
    output: entry.output,
    error: entry.error,
  }
  if (idx >= 0) tools.value[idx] = { ...tools.value[idx], ...next }
  else tools.value.push(next)
  if (entry.status === 'failed') {
    toolsCollapsed.value = false
    localStorage.setItem('ariadne_tools_panel', '1')
    toolsOpenIds.value = new Set([...toolsOpenIds.value, entry.call_id])
  }
}

function toggleToolItem(id: string) {
  const s = new Set(toolsOpenIds.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  toolsOpenIds.value = s
}

function clearTools() {
  tools.value = []
  toolsOpenIds.value = new Set()
}

async function editTitle() {
  if (!sessionId.value) return
  const cur = topTitle.value === 'Ariadne' ? '' : topTitle.value
  const next = window.prompt('会话标题（主题总结）\n留空并确定可自动重总结', cur)
  if (next === null) return
  if (!String(next).trim()) {
    await refreshSessionTitle(true)
    await loadSessions()
    return
  }
  const r = await api(
    '/api/sessions/' + encodeURIComponent(sessionId.value),
    token.value,
    {
      method: 'PATCH',
      body: JSON.stringify({ title: String(next).trim() }),
    },
  )
  if (!r.ok) {
    const d = await r.json().catch(() => ({}))
    window.alert(d.detail || '改标题失败')
    return
  }
  const data = await r.json()
  const row = sessions.value.find((s) => s.session_id === sessionId.value)
  if (row && data.title) row.title = data.title
  await loadSessions()
}

function autoSize() {
  const el = inputEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 180) + 'px'
}

async function send() {
  if (busy.value) return
  const text = input.value.trim()
  if (!text) return
  input.value = ''
  autoSize()
  busy.value = true
  clearTools()
  turnStartedAt = performance.now()

  messages.value.push({ id: 'u-' + Date.now(), role: 'user', content: text })
  const asstId = 'a-' + Date.now()
  messages.value.push({
    id: asstId,
    role: 'assistant',
    content: '',
    thinking: '',
    streaming: true,
    thinkingLive: false,
  })
  await scrollChat()

  const asst = () => messages.value.find((m) => m.id === asstId) as Extract<ChatMsg, { role: 'assistant' }> | undefined

  try {
    const r = await fetch('/api/turns/stream', {
      method: 'POST',
      headers: {
        Authorization: 'Bearer ' + token.value,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ input: text, session_id: sessionId.value }),
    })
    if (!r.ok) {
      let detail = ''
      try {
        detail = (await r.json()).detail || ''
      } catch {
        /* */
      }
      const m = asst()
      if (m) {
        m.content = 'ERROR ' + r.status + (detail ? ': ' + detail : '')
        m.streaming = false
      }
      return
    }
    const reader = r.body!.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const { events, rest } = parseSseBuffer(buf)
      buf = rest
      for (const ev of events) handleEvent(ev, asstId)
      await scrollChat()
    }
  } catch (e) {
    const m = asst()
    if (m) {
      m.content = 'ERROR: ' + (e instanceof Error ? e.message : String(e))
      m.streaming = false
      m.thinkingLive = false
    }
  } finally {
    const m = asst()
    if (m) {
      m.streaming = false
      m.thinkingLive = false
      if (!m.content) m.content = '(empty reply)'
    }
    busy.value = false
    // Refresh topic title then reload session list so sidebar shows the new name.
    await refreshSessionTitle(false)
    await loadSessions()
    // Agent may have written files under /workspace — refresh file browser.
    workspaceRefreshKey.value += 1
    inputEl.value?.focus()
  }
}

function handleEvent(ev: StreamEvent, asstId: string) {
  const m = messages.value.find((x) => x.id === asstId) as
    | Extract<ChatMsg, { role: 'assistant' }>
    | undefined
  if (!m) return
  const data = (ev.data || {}) as Record<string, unknown>

  if (ev.kind === 'model_thinking_delta') {
    m.thinking += String(data.text || '')
    if (!m.content) m.thinkingLive = true
  } else if (ev.kind === 'model_delta') {
    const chunk = String(data.text || '')
    if (chunk && m.thinkingLive) m.thinkingLive = false
    m.content += chunk
  } else if (ev.kind === 'tool_started') {
    upsertTool({
      call_id: String(data.call_id || data.name + '-' + Date.now()),
      name: String(data.name || '?'),
      status: 'started',
    })
  } else if (ev.kind === 'tool_completed') {
    upsertTool({
      call_id: String(data.call_id || data.name + '-' + Date.now()),
      name: String(data.name || '?'),
      status: (data.status as ToolEntry['status']) || 'completed',
      arguments: data.arguments,
      output: data.output,
      error: data.error as ToolEntry['error'],
    })
  } else if (ev.kind === 'turn_completed' || ev.kind === 'turn_failed') {
    if (ev.error?.message) {
      m.content = '**' + (ev.error.code || 'ERROR') + '**\n\n' + ev.error.message
      pushTurnInfo({ status: 'failed' })
    } else if (ev.result && typeof ev.result === 'object') {
      const res = ev.result as {
        text?: string
        status?: string
        error?: { code?: string; message?: string }
        usage?: {
          prompt_tokens?: number
          completion_tokens?: number
          total_tokens?: number
          reasoning_tokens?: number
        }
        tool_calls?: unknown[]
        model?: string
        turn_id?: string
      }
      if (!m.content && res.text) m.content = res.text
      if (res.status && res.status !== 'completed') {
        upsertTool({
          call_id: 'turn-' + Date.now(),
          name: 'turn',
          status: 'failed',
          error: res.error || { code: 'failed', message: 'turn failed' },
        })
      }
      pushTurnInfo(res)
    } else {
      pushTurnInfo({ status: ev.kind === 'turn_failed' ? 'failed' : 'completed' })
    }
    m.streaming = false
    m.thinkingLive = false
  }
}

function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '—'
  if (ms < 1000) return Math.round(ms) + ' ms'
  const s = ms / 1000
  if (s < 60) return s.toFixed(s < 10 ? 2 : 1) + ' s'
  const m = Math.floor(s / 60)
  const rem = s - m * 60
  return m + 'm ' + rem.toFixed(1) + 's'
}

function pushTurnInfo(res: {
  usage?: {
    prompt_tokens?: number
    completion_tokens?: number
    total_tokens?: number
    reasoning_tokens?: number
  }
  tool_calls?: unknown[]
  model?: string
  turn_id?: string
  status?: string
}) {
  const elapsedMs = turnStartedAt ? performance.now() - turnStartedAt : 0
  const usage = res.usage || {}
  const prompt = Number(usage.prompt_tokens || 0)
  const completion = Number(usage.completion_tokens || 0)
  const total = Number(usage.total_tokens || 0) || prompt + completion
  const reasoning = Number(usage.reasoning_tokens || 0)
  // Prefer server tool_calls length; fall back to local non-info tool entries.
  const serverTools = Array.isArray(res.tool_calls) ? res.tool_calls.length : -1
  const localTools = tools.value.filter((t) => t.status !== 'info').length
  const nTools = serverTools >= 0 ? serverTools : localTools
  const nFail = tools.value.filter((t) => t.status === 'failed').length

  const tokenBits = [
    `total ${total}`,
    `prompt ${prompt}`,
    `completion ${completion}`,
  ]
  if (reasoning > 0) tokenBits.push(`reasoning ${reasoning}`)

  const summary =
    `${formatDuration(elapsedMs)} · ${total} tokens · ${nTools} tool${nTools === 1 ? '' : 's'}` +
    (nFail ? ` · ${nFail} failed` : '')

  const details: Record<string, string | number> = {
    duration: formatDuration(elapsedMs),
    'duration_ms': Math.round(elapsedMs),
    tokens_total: total,
    tokens_prompt: prompt,
    tokens_completion: completion,
  }
  if (reasoning > 0) details.tokens_reasoning = reasoning
  details.tool_calls = nTools
  if (nFail) details.tool_failed = nFail
  if (res.model) details.model = res.model
  if (res.turn_id) details.turn_id = String(res.turn_id)
  if (res.status) details.status = res.status
  details.token_breakdown = tokenBits.join(' · ')

  const id = 'info-turn-' + (res.turn_id || Date.now())
  upsertTool({
    call_id: id,
    name: 'info',
    status: 'info',
    summary,
    details,
  })
  // Auto-open stats so it's visible without an extra click.
  toolsOpenIds.value = new Set([...toolsOpenIds.value, id])
  // Surface the panel briefly when there were tools or non-trivial usage.
  if (nTools > 0 || total > 0) {
    toolsCollapsed.value = false
    localStorage.setItem('ariadne_tools_panel', '1')
  }
}

// Provider / plugins modals
const showProvider = ref(false)
const showPlugins = ref(false)
const baseUrl = ref('')
const apiKey = ref('')
const modelName = ref('')
const provMsg = ref('')

async function saveProvider() {
  provMsg.value = ''
  const r = await api('/api/me/provider', token.value, {
    method: 'PUT',
    body: JSON.stringify({
      base_url: baseUrl.value,
      api_key: apiKey.value,
      model: modelName.value,
    }),
  })
  if (!r.ok) {
    const d = await r.json().catch(() => ({}))
    provMsg.value = d.detail || '保存失败'
    return
  }
  showProvider.value = false
  await bootstrap()
}

watch(showProvider, (v) => {
  if (v && me.value) {
    baseUrl.value = me.value.base_url || ''
    modelName.value = me.value.model || ''
    apiKey.value = ''
  }
})

onMounted(() => {
  if (token.value) bootstrap()
})
</script>

<template>
  <AuthView v-if="!authed" @authed="onAuthed" />
  <div
    v-else
    class="shell"
    :class="{ 'sb-collapsed': sidebarCollapsed, 'ws-mode': leftTab === 'workspace' }"
  >
    <aside
      class="sidebar"
      :class="{ collapsed: sidebarCollapsed, 'ws-open': leftTab === 'workspace' }"
    >
      <div class="sb-top">
        <div class="sb-brand"><span class="mark">A</span> Ariadne</div>
        <button type="button" class="new-chat" @click="createSession">
          <span class="plus">+</span> 新对话
        </button>
        <div class="sb-tabs" role="tablist" aria-label="侧栏">
          <button
            type="button"
            role="tab"
            class="sb-tab"
            :class="{ active: leftTab === 'sessions' }"
            :aria-selected="leftTab === 'sessions'"
            @click="setLeftTab('sessions')"
          >
            历史
          </button>
          <button
            type="button"
            role="tab"
            class="sb-tab"
            :class="{ active: leftTab === 'workspace' }"
            :aria-selected="leftTab === 'workspace'"
            @click="setLeftTab('workspace')"
          >
            工作区
          </button>
        </div>
      </div>
      <div v-show="leftTab === 'sessions'" class="sess-list">
        <div v-if="!sessions.length" class="sb-empty">暂无会话</div>
        <button
          v-for="s in sessions"
          :key="s.session_id"
          type="button"
          class="sess-row"
          :class="{ active: s.session_id === sessionId }"
          @click="selectSession(s.session_id)"
        >
          <span class="body">
            <span class="title-line">{{ s.title || s.preview || s.session_id }}</span>
            <span class="meta">{{ s.turns || 0 }} 轮 · {{ fmtTime(s.mtime) }}</span>
          </span>
          <span
            class="del"
            title="删除"
            @click.stop="deleteSession(s.session_id)"
          >×</span>
        </button>
      </div>
      <WorkspaceBrowser
        v-show="leftTab === 'workspace'"
        :token="token"
        :active="leftTab === 'workspace'"
        :refresh-key="workspaceRefreshKey"
      />
      <div class="sb-bottom">
        <button type="button" class="sb-item" @click="toggleTheme">
          <span class="ico">{{ theme === 'light' ? '☾' : '☀' }}</span>
          <span>
            <span>外观</span>
            <span class="sub">{{ theme === 'light' ? '浅色' : '深色' }}</span>
          </span>
        </button>
        <button type="button" class="sb-item" @click="showProvider = true">
          <span class="ico">⚙</span>
          <span>
            <span>Provider</span>
            <span class="sub">{{ me?.model || '未配置' }}</span>
          </span>
        </button>
        <button type="button" class="sb-item" @click="showPlugins = true">
          <span class="ico">▣</span>
          <span>
            <span>插件</span>
            <span class="sub">GitLab / Redmine / Odoo</span>
          </span>
        </button>
        <button type="button" class="sb-item" @click="logout">
          <span class="ico">⎋</span>
          <span>
            <span>{{ username }}</span>
            <span class="sub">退出登录</span>
          </span>
        </button>
      </div>
    </aside>

    <div class="main">
      <div class="topbar">
        <button type="button" class="icon-btn" title="侧栏" @click="toggleSidebar">☰</button>
        <button
          type="button"
          class="icon-btn"
          title="工作区 /workspace"
          :class="{ on: leftTab === 'workspace' && !sidebarCollapsed }"
          @click="setLeftTab(leftTab === 'workspace' ? 'sessions' : 'workspace')"
        >📂</button>
        <span
          class="title"
          title="点击可重命名；留空确定则自动重总结主题"
          @click="editTitle"
        >{{ topTitle }}</span>
        <div class="spacer" />
        <button type="button" class="icon-btn" title="主题" @click="toggleTheme">
          {{ theme === 'light' ? '☾' : '☀' }}
        </button>
        <button type="button" class="icon-btn tools-btn" title="工具" @click="toggleTools">
          ▤
          <span v-if="tools.length" class="badge" :class="{ fail: tools.some((t) => t.status === 'failed') }">
            {{ tools.length }}
          </span>
        </button>
        <span class="chip mono">{{ sessionLabel }}</span>
        <span class="chip" :class="{ off: !providerOk }">
          <span class="dot" />
          {{ me?.model || '未配置' }}
        </span>
      </div>

      <div ref="chatEl" class="chat">
        <div class="chat-inner" :class="{ empty: !messages.length }">
          <div v-if="!messages.length" class="empty-hint">
            <div class="art">A</div>
            <h2>今天想做什么？</h2>
            <p>Vue 前端 · markdown-it 表格/代码 · 流式 thinking 折叠</p>
          </div>
          <template v-for="m in messages" :key="m.id">
            <div v-if="m.role === 'user'" class="msg user">{{ m.content }}</div>
            <div v-else class="msg assistant" :class="{ streaming: m.streaming }">
              <div class="meta">
                <span class="avatar">A</span>
                <span>Ariadne</span>
                <span v-if="m.streaming" class="status">
                  · {{ m.thinkingLive ? '思考中' : m.content ? '生成中' : '思考中' }}
                </span>
              </div>
              <ThinkingBlock :text="m.thinking" :live="m.thinkingLive" />
              <MarkdownView
                v-if="m.content || !m.thinking"
                :source="m.content || (m.streaming ? '' : '(empty reply)')"
                :streaming="m.streaming && !!m.content"
              />
              <div v-else-if="m.streaming && !m.content" class="md-placeholder">…</div>
            </div>
          </template>
          <button
            v-if="tools.length"
            type="button"
            class="tool-chip"
            :class="{ fail: tools.some((t) => t.status === 'failed') }"
            @click="toolsCollapsed = false"
          >
            <span class="n">{{ tools.filter((t) => t.status !== 'info').length }}</span>
            <template v-if="tools.some((t) => t.status === 'info')">
              {{ tools.find((t) => t.status === 'info')?.summary || 'turn stats' }}
            </template>
            <template v-else>工具调用 · 右侧详情</template>
          </button>
        </div>
      </div>

      <div class="composer-wrap">
        <div class="composer">
          <textarea
            ref="inputEl"
            v-model="input"
            rows="1"
            placeholder="给 Ariadne 下达任务…"
            :disabled="busy"
            @input="autoSize"
            @keydown.enter.exact.prevent="send"
          />
          <button type="button" class="send" :disabled="busy || !input.trim()" @click="send">
            ↑
          </button>
        </div>
        <div class="foot">Ariadne · Vue + markdown-it · open-source agent kernel</div>
      </div>
    </div>

    <ToolsPanel
      :entries="tools"
      :collapsed="toolsCollapsed"
      :open-ids="toolsOpenIds"
      @toggle-panel="toggleTools"
      @clear="clearTools"
      @toggle-item="toggleToolItem"
    />

    <div v-if="showProvider" class="modal-backdrop" @click.self="showProvider = false">
      <div class="modal">
        <h3>Provider（BYOK）</h3>
        <label>BASE_URL</label>
        <input v-model="baseUrl" placeholder="https://api.example.com/v1" />
        <label>API_KEY</label>
        <input v-model="apiKey" type="password" placeholder="Bearer token" autocomplete="off" />
        <label>MODEL</label>
        <input v-model="modelName" placeholder="model-id" />
        <p class="warn">{{ provMsg }}</p>
        <div class="modal-actions">
          <button type="button" class="secondary" @click="showProvider = false">取消</button>
          <button type="button" class="primary" @click="saveProvider">保存</button>
        </div>
      </div>
    </div>

    <PluginsModal
      :open="showPlugins"
      :token="token"
      @close="showPlugins = false"
    />
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  height: 100%;
  overflow: hidden;
}
.sidebar {
  width: var(--sidebar-w);
  flex-shrink: 0;
  background: var(--bg-2);
  border-right: 1px solid var(--line);
  display: flex;
  flex-direction: column;
  transition: margin 0.2s, opacity 0.2s, width 0.2s;
}
/* Codex-style: file browser needs more room for tree + preview */
.sidebar.ws-open {
  width: var(--sidebar-ws-w, 380px);
}
.sidebar.collapsed {
  margin-left: calc(-1 * var(--sidebar-w));
  opacity: 0;
  pointer-events: none;
}
.sidebar.collapsed.ws-open {
  margin-left: calc(-1 * var(--sidebar-ws-w, 380px));
}
.sb-top { padding: 14px 12px 8px; display: flex; flex-direction: column; gap: 8px; }
.sb-tabs {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px;
  padding: 3px;
  border-radius: 12px;
  background: var(--bg-3);
  border: 1px solid var(--line);
}
.sb-tab {
  padding: 7px 8px;
  border-radius: 9px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--dim);
}
.sb-tab:hover { color: var(--fg-2); }
.sb-tab.active {
  background: var(--bg-2);
  color: var(--fg);
  box-shadow: 0 0 0 1px var(--line);
}
.sb-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 8px;
  font-weight: 700;
}
.mark {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: var(--btn);
  color: var(--btn-fg);
  display: grid;
  place-items: center;
  font-size: 14px;
  font-weight: 800;
}
.new-chat {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px 12px;
  border-radius: 12px;
  border: 1px solid var(--line-2);
  font-weight: 560;
}
.new-chat:hover { background: var(--bg-hover); }
.plus {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--btn);
  color: var(--btn-fg);
  display: grid;
  place-items: center;
  font-weight: 600;
}
.sb-section {
  padding: 8px 12px 4px;
  font-size: 11px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-weight: 600;
}
.sess-list { flex: 1; overflow-y: auto; padding: 4px 8px 12px; }
.sb-empty { padding: 16px 12px; color: var(--muted); font-size: 13px; }
.sess-row {
  width: 100%;
  text-align: left;
  display: flex;
  gap: 8px;
  padding: 10px;
  border-radius: 12px;
  margin-bottom: 2px;
  color: var(--fg-2);
}
.sess-row:hover { background: var(--bg-hover); }
.sess-row.active { background: var(--bg-3); }
.sess-row .body { flex: 1; min-width: 0; }
.title-line {
  display: block;
  font-size: 13.5px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.meta { display: block; font-size: 11px; color: var(--muted); margin-top: 3px; }
.del {
  opacity: 0;
  padding: 4px 8px;
  border-radius: 8px;
  color: var(--dim);
}
.sess-row:hover .del { opacity: 1; }
.del:hover { color: var(--err); }
.sb-bottom {
  border-top: 1px solid var(--line);
  padding: 10px 8px 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.sb-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px;
  border-radius: 12px;
  color: var(--fg-2);
  font-size: 13.5px;
  text-align: left;
}
.sb-item:hover { background: var(--bg-hover); }
.ico {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: var(--bg-3);
  border: 1px solid var(--line);
  display: grid;
  place-items: center;
  font-size: 13px;
  color: var(--dim);
}
.sub { display: block; font-size: 11px; color: var(--muted); margin-top: 1px; }

.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg);
}
.topbar {
  height: 52px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 16px;
}
.icon-btn {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  color: var(--dim);
  position: relative;
}
.icon-btn:hover { background: var(--bg-hover); color: var(--fg); }
.icon-btn.on { background: var(--bg-3); color: var(--fg); }
.tools-btn .badge {
  position: absolute;
  top: 4px;
  right: 2px;
  min-width: 14px;
  height: 14px;
  padding: 0 3px;
  border-radius: 999px;
  background: var(--blue);
  color: #fff;
  font-size: 9px;
  font-weight: 700;
  line-height: 14px;
}
.tools-btn .badge.fail { background: var(--err); }
.title { font-weight: 600; font-size: 15px; }
.spacer { flex: 1; }
.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  padding: 6px 10px;
  border-radius: 999px;
  border: 1px solid var(--line);
  color: var(--ok);
}
.chip.off { color: var(--warn); }
.chip.mono { font-family: var(--mono); color: var(--muted); }
.chip .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}

.chat {
  flex: 1;
  overflow-y: auto;
}
.chat-inner {
  max-width: 760px;
  margin: 0 auto;
  padding: 12px 20px 8px;
  min-height: 100%;
  display: flex;
  flex-direction: column;
  gap: 22px;
}
.chat-inner.empty {
  justify-content: center;
}
.empty-hint {
  text-align: center;
  padding: 24px 12px;
  color: var(--dim);
}
.art {
  width: 56px;
  height: 56px;
  margin: 0 auto 18px;
  border-radius: 16px;
  background: var(--btn);
  color: var(--btn-fg);
  display: grid;
  place-items: center;
  font-size: 26px;
  font-weight: 800;
}
.empty-hint h2 {
  margin: 0 0 10px;
  font-size: 1.75rem;
  font-weight: 650;
  color: var(--fg);
}
.empty-hint p {
  margin: 0 auto;
  max-width: 420px;
  font-size: 15px;
  line-height: 1.5;
}

.msg.user {
  align-self: flex-end;
  max-width: min(640px, 92%);
  background: var(--bg-3);
  border: 1px solid var(--line);
  border-radius: 22px;
  padding: 12px 16px;
  white-space: pre-wrap;
  line-height: 1.5;
  font-size: 15px;
}
.msg.assistant {
  align-self: stretch;
  padding: 2px 4px 4px;
}
.msg.assistant .meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  color: var(--dim);
  font-size: 13px;
  font-weight: 560;
}
.avatar {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: var(--btn);
  color: var(--btn-fg);
  display: grid;
  place-items: center;
  font-size: 12px;
  font-weight: 800;
}
.md-placeholder { color: var(--muted); font-size: 15px; }
.tool-chip {
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--dim);
  background: var(--bg-3);
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 6px 12px;
}
.tool-chip:hover { background: var(--bg-hover); color: var(--fg); }
.tool-chip.fail { border-color: rgba(244, 33, 46, 0.35); color: var(--err); }
.tool-chip .n {
  color: var(--fg-2);
  font-weight: 600;
  font-family: var(--mono);
}

.composer-wrap { padding: 0 16px 12px; }
.composer {
  max-width: 760px;
  margin: 0 auto;
  display: flex;
  gap: 10px;
  align-items: flex-end;
  background: var(--bg-3);
  border: 1px solid var(--line);
  border-radius: 24px;
  padding: 10px 12px 10px 16px;
}
.composer textarea {
  flex: 1;
  border: 0;
  outline: none;
  background: transparent;
  resize: none;
  max-height: 180px;
  line-height: 1.45;
  font-size: 15px;
}
.send {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background: var(--btn);
  color: var(--btn-fg);
  font-size: 18px;
  font-weight: 600;
  flex-shrink: 0;
}
.send:disabled {
  background: var(--line-2);
  color: var(--muted);
}
.foot {
  max-width: 760px;
  margin: 8px auto 0;
  text-align: center;
  font-size: 11px;
  color: var(--muted);
}

.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  display: grid;
  place-items: center;
  z-index: 50;
  padding: 16px;
}
.modal {
  width: min(420px, 100%);
  background: var(--bg-3);
  border: 1px solid var(--line);
  border-radius: 16px;
  padding: 20px;
}
.modal h3 { margin: 0 0 14px; }
.modal label {
  display: block;
  font-size: 12px;
  color: var(--muted);
  margin: 8px 0 4px;
}
.modal input {
  width: 100%;
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid var(--line-2);
  background: var(--bg);
  outline: none;
}
.modal-actions {
  display: flex;
  gap: 10px;
  justify-content: flex-end;
  margin-top: 16px;
}
.modal-actions button {
  padding: 10px 16px;
  border-radius: 999px;
  font-weight: 600;
}
.primary { background: var(--btn); color: var(--btn-fg); }
.secondary { border: 1px solid var(--line-2); }
.warn { color: var(--warn); font-size: 13px; min-height: 1.2em; }

@media (max-width: 800px) {
  .sidebar {
    position: fixed;
    z-index: 30;
    height: 100%;
    box-shadow: 8px 0 40px rgba(0, 0, 0, 0.5);
  }
}
</style>

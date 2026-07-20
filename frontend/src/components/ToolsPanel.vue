<script setup lang="ts">
import { computed } from 'vue'

export type ToolEntry = {
  call_id: string
  name: string
  status: 'started' | 'completed' | 'failed'
  arguments?: unknown
  output?: unknown
  error?: { code?: string; message?: string }
}

const props = defineProps<{
  entries: ToolEntry[]
  collapsed: boolean
  openIds: Set<string>
}>()

const emit = defineEmits<{
  (e: 'toggle-panel'): void
  (e: 'clear'): void
  (e: 'toggle-item', id: string): void
}>()

const count = computed(() => props.entries.length)
const fails = computed(() => props.entries.filter((t) => t.status === 'failed').length)

function pretty(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}

const list = computed(() => [...props.entries].reverse())
</script>

<template>
  <aside class="tools-panel" :class="{ collapsed }" aria-label="工具调用">
    <div class="tp-head">
      <h2>工具调用</h2>
      <span class="tp-count">{{ count }}</span>
      <button type="button" title="清空" @click="emit('clear')">↺</button>
      <button type="button" title="折叠" @click="emit('toggle-panel')">›</button>
    </div>
    <div class="tp-list">
      <div v-if="!entries.length" class="tp-empty">
        本轮工具调用会显示在这里。<br />点击条目可展开参数与结果。
      </div>
      <div
        v-for="t in list"
        :key="t.call_id"
        class="tp-item"
        :class="{
          open: openIds.has(t.call_id),
          fail: t.status === 'failed',
          ok: t.status === 'completed',
          run: t.status === 'started',
        }"
      >
        <button type="button" class="tp-item-head" @click="emit('toggle-item', t.call_id)">
          <span class="dot" />
          <span class="body">
            <div class="name">{{ t.name }}</div>
            <div class="status-line">
              <template v-if="t.status === 'failed'">
                {{ t.error?.code || t.error?.message || 'failed' }}
              </template>
              <template v-else-if="t.status === 'completed'">completed</template>
              <template v-else>running…</template>
            </div>
          </span>
          <span class="chev">›</span>
        </button>
        <div v-if="openIds.has(t.call_id)" class="tp-item-body">
          <div v-if="t.arguments && Object.keys(t.arguments as object).length" class="tp-sec">
            <div class="tp-sec-label">arguments</div>
            <pre class="tp-pre">{{ pretty(t.arguments) }}</pre>
          </div>
          <div v-if="t.status === 'failed' && t.error" class="tp-sec">
            <div class="tp-sec-label">error</div>
            <div class="tp-err">
              {{ t.error.code || '' }}{{ t.error.message ? ' — ' + t.error.message : '' }}
            </div>
          </div>
          <div v-if="t.output !== undefined && t.output !== null" class="tp-sec">
            <div class="tp-sec-label">output</div>
            <pre class="tp-pre">{{ pretty(t.output) }}</pre>
          </div>
        </div>
      </div>
    </div>
    <div v-if="fails" class="tp-foot">{{ fails }} 失败</div>
  </aside>
</template>

<style scoped>
.tools-panel {
  width: var(--tools-w);
  flex-shrink: 0;
  background: var(--bg-2);
  border-left: 1px solid var(--line);
  display: flex;
  flex-direction: column;
  min-width: 0;
  transition: width 0.2s ease, opacity 0.2s, margin 0.2s;
}
.tools-panel.collapsed {
  width: 0;
  opacity: 0;
  pointer-events: none;
  border-left: 0;
  overflow: hidden;
}
.tp-head {
  height: 52px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 12px;
  border-bottom: 1px solid var(--line);
}
.tp-head h2 {
  margin: 0;
  flex: 1;
  font-size: 13px;
  font-weight: 650;
}
.tp-count {
  font-size: 11px;
  color: var(--muted);
  font-family: var(--mono);
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--line);
}
.tp-head button {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  color: var(--dim);
}
.tp-head button:hover {
  background: var(--bg-hover);
  color: var(--fg);
}
.tp-list {
  flex: 1;
  overflow-y: auto;
  padding: 10px;
}
.tp-empty {
  padding: 24px 12px;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.45;
  text-align: center;
}
.tp-item {
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--bg-3);
  margin-bottom: 8px;
  overflow: hidden;
}
.tp-item.fail { border-color: rgba(244, 33, 46, 0.35); }
.tp-item.run { border-color: rgba(29, 155, 240, 0.35); }
.tp-item-head {
  width: 100%;
  text-align: left;
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  color: var(--fg-2);
  font-size: 13px;
}
.tp-item-head:hover { background: var(--bg-hover); }
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-top: 5px;
  flex-shrink: 0;
  background: var(--blue);
}
.ok .dot { background: var(--ok); }
.fail .dot { background: var(--err); }
.body { flex: 1; min-width: 0; }
.name {
  font-family: var(--mono);
  font-weight: 650;
  font-size: 12.5px;
  word-break: break-all;
}
.status-line {
  font-size: 11px;
  color: var(--muted);
  margin-top: 3px;
}
.fail .status-line { color: var(--err); }
.chev {
  color: var(--muted);
  font-size: 12px;
  transition: transform 0.15s;
}
.open .chev { transform: rotate(90deg); }
.tp-item-body {
  border-top: 1px solid var(--line);
  padding: 10px 12px 12px;
  font-size: 12px;
}
.tp-sec { margin: 0 0 10px; }
.tp-sec-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
  font-weight: 650;
  margin-bottom: 4px;
}
.tp-pre {
  margin: 0;
  padding: 8px 10px;
  background: var(--pre-bg);
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow-x: auto;
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.45;
  color: var(--fg-2);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 220px;
  overflow-y: auto;
}
.tp-err {
  color: var(--err);
  font-family: var(--mono);
  font-size: 12px;
}
.tp-foot {
  padding: 8px 12px;
  border-top: 1px solid var(--line);
  font-size: 12px;
  color: var(--err);
}
</style>

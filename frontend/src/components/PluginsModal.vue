<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import { api } from '../api/client'

export type PluginRow = {
  name: string
  description: string
  required_config: string[]
  enabled: boolean
  configured: boolean
  config: Record<string, string>
}

const props = defineProps<{
  open: boolean
  token: string
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

const list = ref<PluginRow[]>([])
const drafts = reactive<Record<string, Record<string, string>>>({})
const msg = ref('')
const busy = ref(false)

function isSecret(key: string): boolean {
  return /key|token|password|secret/i.test(key)
}

async function load() {
  msg.value = ''
  try {
    const r = await api('/api/me/plugins', props.token)
    if (!r.ok) {
      const d = await r.json().catch(() => ({}))
      msg.value = d.detail || '加载插件失败'
      return
    }
    const rows: PluginRow[] = await r.json()
    list.value = rows
    for (const p of rows) {
      const next: Record<string, string> = {}
      for (const k of p.required_config) {
        next[k] = (p.config && p.config[k]) || ''
      }
      drafts[p.name] = next
    }
  } catch (e) {
    msg.value = e instanceof Error ? e.message : String(e)
  }
}

async function enableOrUpdate(p: PluginRow) {
  if (busy.value) return
  busy.value = true
  msg.value = ''
  try {
    const config = { ...(drafts[p.name] || {}) }
    const r = await api('/api/me/plugins/' + encodeURIComponent(p.name), props.token, {
      method: 'PUT',
      body: JSON.stringify({ config }),
    })
    if (!r.ok) {
      const d = await r.json().catch(() => ({}))
      msg.value = d.detail || `启用 ${p.name} 失败`
      return
    }
    await load()
  } finally {
    busy.value = false
  }
}

async function disable(p: PluginRow) {
  if (busy.value) return
  busy.value = true
  msg.value = ''
  try {
    const r = await api('/api/me/plugins/' + encodeURIComponent(p.name), props.token, {
      method: 'DELETE',
    })
    if (!r.ok) {
      const d = await r.json().catch(() => ({}))
      msg.value = d.detail || `停用 ${p.name} 失败`
      return
    }
    await load()
  } finally {
    busy.value = false
  }
}

watch(
  () => props.open,
  (v) => {
    if (v) load()
  },
)

onMounted(() => {
  if (props.open) load()
})
</script>

<template>
  <div v-if="open" class="modal-backdrop" @click.self="emit('close')">
    <div class="modal" role="dialog" aria-label="插件">
      <h3>官方插件</h3>
      <p class="hint">
        配置保存在当前 Web 账号下。密钥字段显示为脱敏值；保持
        <code>*****</code> 可保留原密钥。
      </p>
      <p v-if="msg" class="warn">{{ msg }}</p>
      <div v-if="!list.length" class="empty">暂无插件定义</div>
      <div v-for="p in list" :key="p.name" class="plugin-card">
        <div class="head">
          <strong>{{ p.name }}</strong>
          <small>{{ p.description }}</small>
          <span class="badge" :class="{ off: !p.enabled }">
            {{ p.enabled ? '已启用' : '未启用' }}
          </span>
        </div>
        <div
          v-for="key in p.required_config"
          :key="p.name + '-' + key"
          class="field"
        >
          <label>{{ key }}</label>
          <input
            v-model="drafts[p.name][key]"
            :type="isSecret(key) && !drafts[p.name][key] ? 'password' : 'text'"
            :placeholder="key"
            :autocomplete="isSecret(key) ? 'new-password' : 'off'"
            :spellcheck="false"
            :class="{ mono: isSecret(key) && !!drafts[p.name][key] }"
            :title="
              isSecret(key) && drafts[p.name][key]
                ? '已脱敏；保持 ***** 可保留原密钥'
                : undefined
            "
          />
        </div>
        <div class="row">
          <button
            type="button"
            class="primary"
            :disabled="busy"
            @click="enableOrUpdate(p)"
          >
            {{ p.enabled ? '更新' : '启用' }}
          </button>
          <button
            v-if="p.enabled"
            type="button"
            class="secondary"
            :disabled="busy"
            @click="disable(p)"
          >
            停用
          </button>
        </div>
      </div>
      <div class="modal-actions">
        <button type="button" class="secondary" @click="emit('close')">关闭</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
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
  width: min(560px, 100%);
  max-height: min(90vh, 720px);
  overflow-y: auto;
  background: var(--bg-3);
  border: 1px solid var(--line);
  border-radius: 16px;
  padding: 20px;
}
.modal h3 {
  margin: 0 0 8px;
}
.hint {
  margin: 0 0 12px;
  font-size: 12.5px;
  color: var(--muted);
  line-height: 1.45;
}
.hint code {
  font-family: var(--mono);
  font-size: 0.92em;
}
.warn {
  color: var(--warn);
  font-size: 13px;
  margin: 0 0 10px;
}
.empty {
  color: var(--muted);
  font-size: 13px;
  padding: 12px 0;
}
.plugin-card {
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 12px 14px;
  margin-bottom: 12px;
  background: var(--bg);
}
.head {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: baseline;
  margin-bottom: 4px;
}
.head strong {
  font-size: 14px;
}
.head small {
  color: var(--dim);
  font-size: 12.5px;
  flex: 1;
  min-width: 120px;
}
.badge {
  font-size: 11px;
  font-weight: 650;
  padding: 2px 8px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--ok) 18%, transparent);
  color: var(--ok);
}
.badge.off {
  background: var(--bg-hover);
  color: var(--muted);
}
.field {
  margin-top: 8px;
}
.field label {
  display: block;
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 4px;
}
.field input {
  width: 100%;
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid var(--line-2);
  background: var(--bg-3);
  outline: none;
}
.field input.mono {
  font-family: var(--mono);
}
.row {
  display: flex;
  gap: 10px;
  margin-top: 10px;
}
.row button {
  padding: 10px 14px;
  border-radius: 999px;
  font-weight: 600;
}
.primary {
  background: var(--btn);
  color: var(--btn-fg);
}
.secondary {
  border: 1px solid var(--line-2);
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 8px;
}
.modal-actions button {
  padding: 10px 16px;
  border-radius: 999px;
  font-weight: 600;
  border: 1px solid var(--line-2);
}
</style>

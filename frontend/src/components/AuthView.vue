<script setup lang="ts">
import { ref } from 'vue'
import { login, register } from '../api/client'

const emit = defineEmits<{
  (e: 'authed', token: string): void
}>()

const username = ref('')
const password = ref('')
const msg = ref('')
const busy = ref(false)

async function doAuth(kind: 'login' | 'register') {
  if (busy.value) return
  msg.value = ''
  busy.value = true
  try {
    const fn = kind === 'login' ? login : register
    const r = await fn(username.value.trim(), password.value)
    const data = await r.json().catch(() => ({}))
    if (!r.ok) {
      msg.value = data.detail || (kind === 'login' ? '登录失败' : '注册失败')
      return
    }
    emit('authed', data.token)
  } catch (e) {
    msg.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="auth">
    <div class="card">
      <div class="logo">A</div>
      <h1>Ariadne</h1>
      <p class="cn-name">筑梦师</p>
      <p class="tag">
        希腊神话中的引线者 · 个人开源 Agent 内核<br />
        Skills 是线 · 作坊是迷宫旁的安全桌面
      </p>
      <input v-model="username" placeholder="用户名" autocomplete="username" />
      <input
        v-model="password"
        type="password"
        placeholder="密码（≥8 位）"
        autocomplete="current-password"
        @keydown.enter="doAuth('login')"
      />
      <div class="actions">
        <button type="button" class="primary" :disabled="busy" @click="doAuth('login')">
          登录
        </button>
        <button type="button" class="secondary" :disabled="busy" @click="doAuth('register')">
          注册
        </button>
      </div>
      <p class="warn">{{ msg }}</p>
    </div>
  </div>
</template>

<style scoped>
.auth {
  min-height: 100%;
  display: grid;
  place-items: center;
  padding: 24px;
  background:
    radial-gradient(800px 400px at 50% -10%, rgba(255, 255, 255, 0.06), transparent 60%),
    var(--bg);
}
.card {
  width: min(400px, 100%);
  background: var(--bg-3);
  border: 1px solid var(--line);
  border-radius: 20px;
  padding: 36px 28px 28px;
}
.cn-name {
  margin: -4px 0 10px;
  text-align: center;
  font-size: 15px;
  font-weight: 600;
  color: var(--fg-2);
  letter-spacing: 0.2em;
}
.tag {
  text-align: center;
  line-height: 1.55;
}
.logo {
  width: 44px;
  height: 44px;
  border-radius: 12px;
  background: var(--btn);
  color: var(--btn-fg);
  display: grid;
  place-items: center;
  font-weight: 800;
  font-size: 20px;
  margin-bottom: 18px;
}
h1 {
  margin: 0 0 6px;
  font-size: 1.5rem;
  font-weight: 700;
}
.tag {
  color: var(--dim);
  font-size: 14px;
  margin: 0 0 24px;
  line-height: 1.45;
}
input {
  width: 100%;
  padding: 12px 14px;
  margin: 0 0 10px;
  background: var(--bg);
  border: 1px solid var(--line-2);
  border-radius: 12px;
  outline: none;
}
.actions {
  display: flex;
  gap: 10px;
  margin-top: 8px;
}
.actions button {
  flex: 1;
  padding: 12px;
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
.warn {
  color: var(--warn);
  font-size: 13px;
  min-height: 1.2em;
  margin: 12px 0 0;
}
</style>

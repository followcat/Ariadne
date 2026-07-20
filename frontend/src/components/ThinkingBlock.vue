<script setup lang="ts">
import { computed, ref, watch } from 'vue'

const props = defineProps<{
  text: string
  live?: boolean
}>()

const open = ref(false)

watch(
  () => props.live,
  (live) => {
    // Live: force expanded visually via CSS .live; after done stay collapsed
    if (!live) open.value = false
  },
)

const label = computed(() => (props.live ? '思考中' : '已思考'))
const visible = computed(() => !!(props.text && props.text.trim()))
</script>

<template>
  <div
    v-if="visible"
    class="thinking"
    :class="{ live: live, open: open && !live }"
  >
    <button
      type="button"
      class="thinking-toggle"
      @click="!live && (open = !open)"
    >
      <span class="ico">✦</span>
      <span class="label">{{ label }}</span>
      <span class="chev">›</span>
    </button>
    <div class="thinking-body">{{ text }}</div>
  </div>
</template>

<style scoped>
.thinking {
  margin: 0 0 10px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--bg-3);
  overflow: hidden;
}
.thinking-toggle {
  width: 100%;
  text-align: left;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  font-size: 13px;
  color: var(--dim);
  font-weight: 560;
}
.thinking-toggle:hover {
  background: var(--bg-hover);
  color: var(--fg-2);
}
.ico {
  width: 18px;
  height: 18px;
  border-radius: 6px;
  display: grid;
  place-items: center;
  font-size: 11px;
  background: var(--bg-hover);
  color: var(--muted);
}
.live .ico { color: var(--blue); }
.label { flex: 1; }
.chev {
  font-size: 12px;
  color: var(--muted);
  transition: transform 0.15s ease;
}
.open .chev { transform: rotate(90deg); }
.thinking-body {
  display: none;
  padding: 0 12px 10px;
  max-height: 220px;
  overflow-y: auto;
  font-size: 13px;
  line-height: 1.55;
  color: var(--muted);
  white-space: pre-wrap;
  word-break: break-word;
  border-top: 1px solid transparent;
}
.open .thinking-body,
.live .thinking-body {
  display: block;
  border-top-color: var(--line);
  padding-top: 8px;
}
.live .label::after {
  content: '';
  display: inline-block;
  width: 5px;
  height: 5px;
  margin-left: 8px;
  border-radius: 50%;
  background: var(--blue);
  animation: blink 1s step-end infinite;
  vertical-align: middle;
}
@keyframes blink {
  50% { opacity: 0; }
}
</style>

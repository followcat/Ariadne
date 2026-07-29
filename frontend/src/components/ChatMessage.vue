<script setup lang="ts">
/**
 * Lazy chat bubble: off-screen assistant messages stay as lightweight placeholders
 * until they approach the scroll viewport (IntersectionObserver).
 */
import {
  computed,
  inject,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
  type Ref,
} from 'vue'
import MarkdownView from './MarkdownView.vue'
import ThinkingBlock from './ThinkingBlock.vue'
import { contentHasDiagrams } from '../lib/mermaid'

export type ChatMsg =
  | {
      id: string
      role: 'user'
      content: string
      turnId?: string
      turnIndex?: number
      toolCount?: number
    }
  | {
      id: string
      role: 'assistant'
      content: string
      thinking: string
      streaming: boolean
      thinkingLive: boolean
      turnId?: string
      turnIndex?: number
      toolCount?: number
    }

const props = defineProps<{
  msg: ChatMsg
  /** Force full render (e.g. last few messages / streaming). */
  eager?: boolean
  /** Highlight turn badge when tools for this turn are shown. */
  activeTurn?: boolean
}>()

const emit = defineEmits<{
  (e: 'open-turn', payload: { turnId?: string; turnIndex?: number }): void
}>()

const chatScrollRoot = inject<Ref<HTMLElement | null> | null>('chatScrollRoot', null)

const shell = ref<HTMLElement | null>(null)
const revealed = ref(false)
let io: IntersectionObserver | null = null

const isUser = computed(() => props.msg.role === 'user')
const asst = computed(() =>
  props.msg.role === 'assistant' ? props.msg : null,
)
const turnIndex = computed(() => props.msg.turnIndex || 0)
const toolCount = computed(() => props.msg.toolCount ?? 0)
const showTurnBadge = computed(() => turnIndex.value > 0)
const turnTitle = computed(() => {
  const n = turnIndex.value
  const tc = toolCount.value
  if (tc > 0) return `第 ${n} 轮 · ${tc} 次工具调用（点击查看）`
  return `第 ${n} 轮`
})

function onTurnClick() {
  if (!showTurnBadge.value) return
  emit('open-turn', {
    turnId: props.msg.turnId,
    turnIndex: props.msg.turnIndex,
  })
}

/** Diagrams must mount MarkdownView or mermaid/SVG never paint after history reload. */
const hasDiagrams = computed(() => contentHasDiagrams(props.msg.content || ''))

const shouldRenderFull = computed(() => {
  if (props.eager) return true
  if (hasDiagrams.value) return true
  if (asst.value?.streaming) return true
  return revealed.value
})

/** Plain preview for placeholder — no markdown work. */
const previewText = computed(() => {
  const c = props.msg.content || ''
  const one = c.replace(/\s+/g, ' ').trim()
  if (one.length <= 160) return one
  return one.slice(0, 160) + '…'
})

/** Rough min-height so scroll position doesn't jump too hard. */
const placeholderMinH = computed(() => {
  const n = (props.msg.content || '').length
  if (n < 200) return 56
  if (n < 800) return 96
  if (n < 3000) return 140
  return 200
})

function disconnectIo() {
  if (io) {
    io.disconnect()
    io = null
  }
}

function setupIo() {
  disconnectIo()
  if (shouldRenderFull.value) return
  const el = shell.value
  if (!el) return
  const root = chatScrollRoot?.value ?? null
  io = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        if (e.isIntersecting) {
          revealed.value = true
          disconnectIo()
          break
        }
      }
    },
    {
      root,
      // Preload a bit above/below viewport
      rootMargin: '480px 0px',
      threshold: 0,
    },
  )
  io.observe(el)
}

onMounted(async () => {
  if (props.eager || hasDiagrams.value || asst.value?.streaming) {
    revealed.value = true
    return
  }
  await nextTick()
  setupIo()
})

watch(
  () =>
    [props.eager, hasDiagrams.value, asst.value?.streaming, chatScrollRoot?.value] as const,
  async ([eager, diagrams, streaming]) => {
    if (eager || diagrams || streaming) {
      revealed.value = true
      disconnectIo()
      return
    }
    if (!revealed.value) {
      await nextTick()
      setupIo()
    }
  },
)

onBeforeUnmount(() => disconnectIo())
</script>

<template>
  <div
    v-if="isUser"
    ref="shell"
    class="msg user"
  >
    <button
      v-if="showTurnBadge"
      type="button"
      class="turn-badge user-turn"
      :class="{ active: activeTurn, 'has-tools': toolCount > 0 }"
      :title="turnTitle"
      @click="onTurnClick"
    >
      <span class="tn">#{{ turnIndex }}</span>
      <span v-if="toolCount > 0" class="tc">{{ toolCount }}</span>
    </button>
    <div class="user-body">{{ msg.content }}</div>
  </div>
  <div
    v-else
    ref="shell"
    class="msg assistant"
    :class="{ streaming: asst?.streaming, lazy: !shouldRenderFull }"
  >
    <div class="meta">
      <span class="avatar">A</span>
      <span>Ariadne</span>
      <button
        v-if="showTurnBadge"
        type="button"
        class="turn-badge"
        :class="{ active: activeTurn, 'has-tools': toolCount > 0 }"
        :title="turnTitle"
        @click="onTurnClick"
      >
        <span class="tn">第 {{ turnIndex }} 轮</span>
        <span v-if="toolCount > 0" class="tc">🔧{{ toolCount }}</span>
      </button>
      <span v-if="asst?.streaming" class="status">
        · {{ asst.thinkingLive ? '思考中' : asst.content ? '生成中' : '思考中' }}
      </span>
    </div>
    <template v-if="shouldRenderFull">
      <ThinkingBlock :text="asst?.thinking || ''" :live="!!asst?.thinkingLive" />
      <MarkdownView
        v-if="asst && (asst.content || !asst.thinking)"
        :source="
          asst.content ||
          (asst.streaming ? '' : '这轮好像没说完，再说一句就好～')
        "
        :streaming="!!asst.streaming && !!asst.content"
        :lite="!asst.streaming"
      />
      <div v-else-if="asst?.streaming && !asst.content" class="md-placeholder">…</div>
    </template>
    <div
      v-else
      class="lazy-ph"
      :style="{ minHeight: placeholderMinH + 'px' }"
    >
      <p class="lazy-preview">{{ previewText || '…' }}</p>
      <p class="lazy-hint">滚动到此处加载完整内容</p>
    </div>
  </div>
</template>

<style scoped>
/* Match App.vue chat bubble look (scoped in child). */
.msg.user {
  align-self: flex-end;
  max-width: min(100%, 640px);
  margin-left: auto;
  padding: 12px 16px;
  border-radius: 18px 18px 4px 18px;
  background: var(--bg-3);
  border: 1px solid var(--line);
  font-size: 15px;
  line-height: 1.55;
  position: relative;
}
.msg.user .user-body {
  white-space: pre-wrap;
  word-break: break-word;
}
.msg.user .turn-badge.user-turn {
  position: absolute;
  top: -10px;
  left: -4px;
}
.msg.assistant {
  max-width: min(100%, 720px);
}
.msg.assistant .meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--dim);
  margin-bottom: 6px;
  flex-wrap: wrap;
}
.turn-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 1px solid var(--line);
  background: var(--bg-3);
  color: var(--dim);
  border-radius: 999px;
  padding: 2px 8px;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  line-height: 1.3;
  font-variant-numeric: tabular-nums;
}
.turn-badge:hover {
  border-color: color-mix(in srgb, var(--blue) 50%, var(--line));
  color: var(--fg);
  background: var(--bg-hover, var(--bg-3));
}
.turn-badge.has-tools {
  border-color: color-mix(in srgb, var(--blue) 35%, var(--line));
  color: var(--blue);
}
.turn-badge.active {
  background: color-mix(in srgb, var(--blue) 22%, transparent);
  border-color: var(--blue);
  color: var(--blue);
}
.turn-badge .tc {
  font-size: 10px;
  opacity: 0.9;
  padding: 0 4px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--blue) 15%, transparent);
}
.avatar {
  width: 22px;
  height: 22px;
  border-radius: 7px;
  background: var(--btn);
  color: var(--btn-fg);
  display: grid;
  place-items: center;
  font-size: 11px;
  font-weight: 800;
}
.status { color: var(--muted); }
.lazy-ph {
  padding: 4px 0 8px;
  border-radius: 10px;
}
.lazy-preview {
  margin: 0;
  font-size: 14.5px;
  line-height: 1.55;
  color: var(--dim);
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.lazy-hint {
  margin: 8px 0 0;
  font-size: 11.5px;
  color: var(--muted);
}
.msg.assistant.lazy {
  opacity: 0.92;
}
.md-placeholder {
  color: var(--muted);
  letter-spacing: 0.15em;
}
</style>

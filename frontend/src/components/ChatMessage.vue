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

export type ChatMsg =
  | { id: string; role: 'user'; content: string }
  | {
      id: string
      role: 'assistant'
      content: string
      thinking: string
      streaming: boolean
      thinkingLive: boolean
    }

const props = defineProps<{
  msg: ChatMsg
  /** Force full render (e.g. last few messages / streaming). */
  eager?: boolean
}>()

const chatScrollRoot = inject<Ref<HTMLElement | null> | null>('chatScrollRoot', null)

const shell = ref<HTMLElement | null>(null)
const revealed = ref(false)
let io: IntersectionObserver | null = null

const isUser = computed(() => props.msg.role === 'user')
const asst = computed(() =>
  props.msg.role === 'assistant' ? props.msg : null,
)

const shouldRenderFull = computed(() => {
  if (props.eager) return true
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
  if (props.eager || asst.value?.streaming) {
    revealed.value = true
    return
  }
  await nextTick()
  setupIo()
})

watch(
  () => [props.eager, asst.value?.streaming, chatScrollRoot?.value] as const,
  async ([eager, streaming]) => {
    if (eager || streaming) {
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
    {{ msg.content }}
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
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 15px;
  line-height: 1.55;
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

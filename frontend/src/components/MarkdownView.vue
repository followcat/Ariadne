<script setup lang="ts">
import { computed, watch, nextTick, ref } from 'vue'
import { renderMarkdown } from '../lib/markdown'

const props = defineProps<{
  source: string
  streaming?: boolean
}>()

const el = ref<HTMLElement | null>(null)

const html = computed(() => renderMarkdown(props.source || ''))

// Wrap tables for horizontal scroll (agent tables can be wide)
watch(
  html,
  async () => {
    await nextTick()
    const root = el.value
    if (!root) return
    root.querySelectorAll('table').forEach((table) => {
      if (table.parentElement?.classList.contains('table-wrap')) return
      const wrap = document.createElement('div')
      wrap.className = 'table-wrap'
      table.parentNode?.insertBefore(wrap, table)
      wrap.appendChild(table)
    })
  },
  { flush: 'post' },
)
</script>

<template>
  <div
    ref="el"
    class="md-body"
    :class="{ 'streaming-answer': streaming && source }"
    v-html="html"
  />
</template>

/// <reference types="vite/client" />

declare module 'markdown-it-multimd-table' {
  import type MarkdownIt from 'markdown-it'
  const plugin: MarkdownIt.PluginWithOptions<Record<string, unknown>>
  export default plugin
}

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<object, object, unknown>
  export default component
}

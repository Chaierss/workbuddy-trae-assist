<script setup lang="ts">
import { computed, ref, type Component } from "vue";
import { BookOpen, Boxes, ListChecks, Plug } from "lucide-vue-next";
import ApiDocsTab from "./ApiDocsTab.vue";
import ComponentsTab from "./ComponentsTab.vue";
import RulesTab from "./RulesTab.vue";
import WikiTab from "./WikiTab.vue";
import { t, type MessageKey } from "../../lib/i18n";

/**
 * 设置 → 规则与文档：规则 / 组件 / 接口文档 / Wiki 四个只读页合成一栏。
 * 四者都是「查资料」，以前各占一格；真正天天点的设置项反而被挤到后面。
 * 页面自己管滚动：文档类内容需要整高，容器不再套一层内边距。
 */
interface DocTab {
  key: string;
  labelKey: MessageKey;
  hintKey: MessageKey;
  icon: Component;
  component: Component;
  /** 需要纵向滚动的 Markdown / 列表页；接口文档与 Wiki 自己管滚动。 */
  scroll?: boolean;
}

const tabs: DocTab[] = [
  {
    key: "rules",
    labelKey: "settings.rules.label",
    hintKey: "settings.rules.hint",
    icon: ListChecks,
    component: RulesTab,
    scroll: true,
  },
  {
    key: "components",
    labelKey: "settings.components.label",
    hintKey: "settings.components.hint",
    icon: Boxes,
    component: ComponentsTab,
    scroll: true,
  },
  {
    key: "api",
    labelKey: "settings.api.label",
    hintKey: "settings.api.hint",
    icon: Plug,
    component: ApiDocsTab,
  },
  {
    key: "wiki",
    labelKey: "settings.wiki.label",
    hintKey: "settings.wiki.hint",
    icon: BookOpen,
    component: WikiTab,
  },
];

const activeKey = ref(tabs[0].key);
const active = computed(() => tabs.find((tab) => tab.key === activeKey.value) ?? tabs[0]);
</script>

<template>
  <div class="flex h-full min-h-0 flex-col bg-surface">
    <header class="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line-soft px-4 py-2.5">
      <div class="flex gap-0.5 rounded-control border border-line p-0.5">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          type="button"
          class="inline-flex items-center gap-1.5 rounded-badge px-2.5 py-1 text-body-sm transition-colors"
          :class="
            activeKey === tab.key
              ? 'bg-brand text-white'
              : 'text-ink-soft hover:bg-line-soft hover:text-ink'
          "
          @click="activeKey = tab.key"
        >
          <component :is="tab.icon" :size="13" :stroke-width="1.9" />
          {{ t(tab.labelKey) }}
        </button>
      </div>
      <p class="min-w-0 flex-1 truncate text-caption text-muted">{{ t(active.hintKey) }}</p>
    </header>

    <div
      class="min-h-0 flex-1"
      :class="active.scroll ? 'overflow-y-auto p-4' : 'overflow-hidden'"
    >
      <KeepAlive>
        <component :is="active.component" />
      </KeepAlive>
    </div>
  </div>
</template>

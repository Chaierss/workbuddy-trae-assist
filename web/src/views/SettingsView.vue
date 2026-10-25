<script setup lang="ts">
import { computed, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { t } from "../lib/i18n";
import {
  SETTINGS_ROUTE,
  findSettingsTab,
  resolveSettingsKey,
  visibleSettingsTabs,
} from "./settings/tabs";

/**
 * 设置容器。左栏由壳层渲染（`SettingsRail`），这里只管右侧内容。
 *
 * 形态来自 `route.meta.focus = 'settings'`（子页 ≥ 2 自动进入专注模式），
 * 不存本地 state —— 否则刷新页面布局就丢了。
 */
const route = useRoute();
const router = useRouter();

/** 桌面端与浏览器的分栏不同，左右两栏必须用同一份过滤结果。 */
const tabs = visibleSettingsTabs();

const activeKey = computed(() => resolveSettingsKey(route.query.tab));
const activeTab = computed(() => findSettingsTab(activeKey.value) ?? tabs[0]);

/** iframe 与双栏文档自己管滚动，容器不再套滚动条与内边距。 */
const selfScroll = computed(() => activeTab.value.selfScroll === true);

// 非法 ?tab= 归一成默认项，别让地址栏留着一个查无此项的值。
// 桌面端把 ?tab=api 也当成非法项处理（那个分栏在桌面端不存在）。
watch(
  activeKey,
  (key) => {
    if (route.query.tab !== key) {
      void router.replace({ path: SETTINGS_ROUTE, query: { ...route.query, tab: key } });
    }
  },
  { immediate: true },
);
</script>

<template>
  <div class="flex h-full min-h-0 flex-col">
    <header v-if="!selfScroll" class="shrink-0 pb-4">
      <h2 class="text-title font-semibold tracking-tight text-ink">{{ t(activeTab.labelKey) }}</h2>
      <p class="mt-0.5 text-body-sm text-muted">{{ t(activeTab.hintKey) }}</p>
    </header>

    <div
      v-if="selfScroll"
      class="min-h-0 flex-1 overflow-hidden rounded-card border border-line bg-surface"
    >
      <KeepAlive>
        <component :is="activeTab.component" />
      </KeepAlive>
    </div>
    <div v-else class="min-h-0 flex-1 overflow-y-auto pr-1">
      <KeepAlive>
        <component :is="activeTab.component" />
      </KeepAlive>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";
import { t } from "../../lib/i18n";
import ModeBadge from "../../components/ModeBadge.vue";
import { SETTINGS_ROUTE, resolveSettingsKey, visibleSettingsTabs } from "./tabs";

/** 壳层在 `shell` 态渲染这个左栏（240px），设置页自己只负责右侧内容。 */
const route = useRoute();
const router = useRouter();

/** 与 SettingsView 共用同一份过滤：桌面端不显示接口文档 / Wiki。 */
const tabs = visibleSettingsTabs();

const activeKey = computed(() => resolveSettingsKey(route.query.tab));

function openTab(key: string): void {
  // replace：切 tab 不该把浏览器的后退栈塞满。
  void router.replace({ path: SETTINGS_ROUTE, query: { tab: key } });
}

function backToMain(): void {
  void router.push("/overview");
}
</script>

<template>
  <aside
    class="fixed inset-y-0 left-0 z-[200] flex w-[var(--sider-width)] flex-col border-r border-line bg-surface"
  >
    <button
      type="button"
      class="block w-full border-b border-line-soft px-4 pb-3 pt-5 text-left transition-colors hover:bg-line-soft"
      @click="backToMain"
    >
      <h1 class="text-body font-semibold tracking-tight text-ink">{{ t("settings.rail.title") }}</h1>
      <p class="mt-0.5 text-caption text-muted">{{ t("settings.rail.subtitle") }}</p>
    </button>

    <nav class="flex-1 space-y-0.5 overflow-y-auto px-2 pb-3 pt-3">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        class="flex w-full items-start gap-2.5 rounded-control px-2.5 py-2 text-left transition-colors"
        :class="
          activeKey === tab.key
            ? 'bg-brand-soft text-brand-dark'
            : 'text-ink-soft hover:bg-line-soft hover:text-ink'
        "
        @click="openTab(tab.key)"
      >
        <component :is="tab.icon" :size="15" :stroke-width="1.9" class="mt-0.5 shrink-0" />
        <span class="min-w-0 flex-1">
          <span class="block truncate text-body-sm" :class="activeKey === tab.key ? 'font-medium' : ''">
            {{ t(tab.labelKey) }}
          </span>
          <span class="mt-0.5 block text-caption leading-snug text-muted">{{ t(tab.hintKey) }}</span>
        </span>
      </button>
    </nav>

    <!-- 模式角标常驻左下角：四件套的哪些内容可见由它决定，不能藏起来 -->
    <div class="shrink-0 border-t border-line-soft p-2">
      <ModeBadge />
    </div>
  </aside>
</template>

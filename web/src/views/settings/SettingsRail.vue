<script setup lang="ts">
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";
import { LayoutGrid } from "lucide-vue-next";
import { t } from "../../lib/i18n";
import { APP_VERSION } from "../../lib/version";
import { SETTINGS_ROUTE, resolveSettingsKey, visibleSettingsTabs } from "./tabs";

/** 壳层在 `shell` 态渲染这个左栏（240px），设置页自己只负责右侧内容。 */
const route = useRoute();
const router = useRouter();

/** 与 SettingsView 共用同一份过滤：桌面端没有接口文档 / Wiki，多一个更新。 */
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
    <div class="px-4 pb-2 pt-5">
      <h1 class="text-[13px] font-semibold tracking-tight text-ink">{{ t("settings.rail.title") }}</h1>
      <p class="mt-0.5 text-[11.5px] text-muted">{{ t("settings.rail.subtitle") }}</p>
    </div>

    <nav class="flex-1 space-y-0.5 overflow-y-auto px-2 pb-3">
      <!-- 返回主菜单并进菜单列表：不再单独占一条顶栏，图标也和其他导航项对齐。 -->
      <button
        type="button"
        class="flex w-full items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-left text-ink-soft transition-colors hover:bg-line-soft hover:text-ink"
        @click="backToMain"
      >
        <LayoutGrid :size="15" :stroke-width="1.9" class="shrink-0" />
        <span class="min-w-0 flex-1 truncate text-[12.5px]">
          {{ t("settings.rail.backToMain") }}
        </span>
      </button>
      <div class="!my-1.5 border-t border-line-soft" />

      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        class="flex w-full items-start gap-2.5 rounded-[9px] px-2.5 py-2 text-left transition-colors"
        :class="
          activeKey === tab.key
            ? 'bg-brand-soft text-brand-dark'
            : 'text-ink-soft hover:bg-line-soft hover:text-ink'
        "
        @click="openTab(tab.key)"
      >
        <component :is="tab.icon" :size="15" :stroke-width="1.9" class="mt-0.5 shrink-0" />
        <span class="min-w-0 flex-1">
          <span class="block truncate text-[12.5px]" :class="activeKey === tab.key ? 'font-medium' : ''">
            {{ t(tab.labelKey) }}
          </span>
          <span class="mt-0.5 block text-[11px] leading-snug text-muted">{{ t(tab.hintKey) }}</span>
        </span>
      </button>
    </nav>

    <div class="shrink-0 border-t border-line-soft px-4 py-2.5 text-[11px] text-muted">
      {{ APP_VERSION }} · {{ t("settings.rail.footer") }}
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { RouterLink } from "vue-router";
import { Moon, Sun } from "lucide-vue-next";
import { t, type MessageKey } from "../lib/i18n";
import { NAV_DESTINATIONS, SETTINGS_DESTINATION } from "../lib/nav";
import {
  accountCount,
  resolvedTheme,
  scopedPath,
  setTheme,
  theme,
  type ThemeMode,
} from "../stores/app";

const props = defineProps<{ collapsed: boolean }>();
const emit = defineEmits<{ "update:collapsed": [value: boolean] }>();

/**
 * 主侧栏只放高频业务（清单见 lib/nav.ts）。低频项统一收进左下角的
 * 「系统设置」按钮，侧栏不被这些项目占位。
 *
 * 地址带上当前作用域（`/s/<scope>/…`）：这样刷新、收藏、分享链接都能带回同一个作用域。
 * 不带前缀的旧地址仍然可用（见 router.ts 的 alias）。
 */
const items = computed(() =>
  NAV_DESTINATIONS.map((item) => ({
    to: scopedPath(item.path),
    label: t(item.labelKey),
    icon: item.icon,
    // 只有号池带计数徽标：它回答「池子里现在有几个账号」。
    badge: item.key === "accounts" ? accountCount.value : 0,
  })),
);

/** 左下角主题按钮：显示当前模式，点一下在浅色/深色之间切换（跟随系统仍去外观里选）。 */
const THEME_LABEL: Record<ThemeMode, MessageKey> = {
  light: "theme.light",
  dark: "theme.dark",
  system: "theme.system",
};
const themeLabel = computed(() => t(THEME_LABEL[theme.value]));
const isDark = computed(() => resolvedTheme() === "dark");

function toggleTheme(): void {
  setTheme(resolvedTheme() === "dark" ? "light" : "dark");
}

function toggleCollapsed(): void {
  emit("update:collapsed", !props.collapsed);
}
</script>

<template>
  <aside
    class="fixed inset-y-0 left-0 z-[200] flex w-[var(--nav-rail-current)] flex-col border-r border-line bg-surface transition-[width] duration-200 ease-out"
  >
    <div class="flex h-14 shrink-0 items-center gap-2.5 border-b border-line-soft px-3">
      <!-- 品牌块本身就是折叠按钮：logo 图标即展开/收起入口，不再单独摆一个折叠箭头 -->
      <button
        type="button"
        class="grid size-9 shrink-0 place-items-center overflow-hidden rounded-[11px] border border-line-soft bg-white transition-opacity hover:opacity-90"
        :class="collapsed ? 'mx-auto' : ''"
        :title="collapsed ? t('shell.expandRail') : t('shell.collapseRail')"
        @click="toggleCollapsed"
      >
        <img src="/android-chrome-192x192.png" alt="" class="size-full object-contain p-[3px]" />
      </button>

      <template v-if="!collapsed">
        <div class="min-w-0 flex-1">
          <div class="truncate text-[14px] font-semibold tracking-tight">chaierww</div>
          <div class="truncate text-[11px] text-muted">{{ t("shell.tagline") }}</div>
        </div>
      </template>
    </div>

    <nav class="flex-1 overflow-y-auto px-2 py-3">
      <div
        v-if="!collapsed"
        class="px-2.5 pb-1.5 text-[10.5px] font-medium uppercase tracking-[0.09em] text-muted"
      >
        {{ t("shell.sectionWorkbench") }}
      </div>
      <div class="space-y-0.5">
        <RouterLink
          v-for="item in items"
          :key="item.to"
          v-slot="{ navigate, isActive }"
          :to="item.to"
          custom
        >
          <button
            type="button"
            class="relative flex w-full items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-[13px] transition-[background-color,color] duration-150"
            :class="
              isActive
                ? 'bg-brand-soft font-medium text-brand-dark'
                : 'text-ink-soft hover:bg-line-soft hover:text-ink'
            "
            :title="collapsed ? item.label : undefined"
            @click="navigate"
          >
            <span
              v-if="isActive"
              class="absolute left-0 top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-full bg-brand"
            />
            <component :is="item.icon" :size="17" :stroke-width="1.9" class="shrink-0" />
            <template v-if="!collapsed">
              <span class="flex-1 truncate text-left">{{ item.label }}</span>
              <span
                v-if="item.badge"
                class="rounded-full px-1.5 py-0.5 text-[11px] leading-none tabular-nums"
                :class="isActive ? 'bg-white/80 text-brand-dark dark:bg-white/15' : 'bg-line-soft text-muted'"
              >
                {{ item.badge }}
              </span>
            </template>
          </button>
        </RouterLink>
      </div>
    </nav>

    <!-- 左下角：系统设置 + 主题切换并排。折叠时只留两个图标按钮。 -->
    <div
      class="flex shrink-0 gap-1.5 border-t border-line-soft p-2"
      :class="collapsed ? 'flex-col items-center' : ''"
    >
      <RouterLink
        :to="SETTINGS_DESTINATION.path"
        custom
        v-slot="{ navigate, isActive }"
      >
        <button
          type="button"
          class="flex min-w-0 items-center justify-center gap-1.5 rounded-[9px] border px-2 py-2 text-[12px] transition-colors"
          :class="[
            collapsed ? 'size-9 p-0' : 'flex-1',
            isActive
              ? 'border-brand bg-brand-soft text-brand-dark'
              : 'border-line bg-surface text-ink-soft hover:border-[#d5d8d3] hover:text-ink',
          ]"
          :title="t(SETTINGS_DESTINATION.labelKey)"
          @click="navigate"
        >
          <component :is="SETTINGS_DESTINATION.icon" :size="15" :stroke-width="1.9" class="shrink-0" />
          <span v-if="!collapsed" class="truncate">{{ t(SETTINGS_DESTINATION.labelKey) }}</span>
        </button>
      </RouterLink>

      <button
        type="button"
        class="flex min-w-0 items-center justify-center gap-1.5 rounded-[9px] border border-line bg-surface px-2 py-2 text-[12px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink"
        :class="collapsed ? 'size-9 p-0' : 'flex-1'"
        :title="t('shell.toggleTheme', { mode: themeLabel })"
        @click="toggleTheme"
      >
        <Moon v-if="isDark" :size="15" :stroke-width="1.9" class="shrink-0" />
        <Sun v-else :size="15" :stroke-width="1.9" class="shrink-0" />
        <span v-if="!collapsed" class="truncate">{{ themeLabel }}</span>
      </button>
    </div>
  </aside>
</template>

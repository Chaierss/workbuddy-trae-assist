<script setup lang="ts">
import { computed } from "vue";
import { RouterLink } from "vue-router";
import { Moon, Sun } from "lucide-vue-next";
import { t } from "../lib/i18n";
import { NAV_DESTINATIONS, SETTINGS_DESTINATION } from "../lib/nav";
import {
  accountCount,
  health,
  resolvedTheme,
  scopedPath,
  setTheme,
} from "../stores/app";
import { updateBadge, updateState } from "../stores/update";

const props = defineProps<{ collapsed: boolean }>();
const emit = defineEmits<{ "update:collapsed": [value: boolean] }>();

/**
 * 主侧栏只放高频业务（清单见 lib/nav.ts）。低频项统一收进左下角的
 * 「设置」按钮，侧栏不被这些项目占位。
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

/**
 * 主题按钮：纯图标，点一下在浅色/深色之间切换（跟随系统仍去外观里选）。
 * 图标本身表达「当前是暗色/亮色」，title 补充当前模式与下一步动作。
 */
const isDark = computed(() => resolvedTheme() === "dark");
const themeTitle = computed(() =>
  isDark.value ? t("shell.themeToLight") : t("shell.themeToDark"),
);

function toggleTheme(): void {
  setTheme(resolvedTheme() === "dark" ? "light" : "dark");
}

function toggleCollapsed(): void {
  emit("update:collapsed", !props.collapsed);
}

/** 更新小圆点的 title：给一个比「有新版本」更具体的提示。 */
const updateBadgeTitle = computed(() => {
  const s = updateState.value;
  if (!s || !updateBadge.value) return "";
  if (s.status === "downloaded") {
    return t("update.badge.downloaded", { version: s.version ?? "" });
  }
  return t("update.badge.available", { version: s.version ?? "" });
});
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
          <!-- 版本号只有一个来源：后端 /api/health 报的 __version__。
               以前这里和 web/src/lib/version.ts 各写一份字面量，两处都停在 0.1.0。 -->
          <div v-if="health?.version" class="truncate text-[10.5px] text-muted">
            v{{ health.version }}
          </div>
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

    <!-- 左下角：设置（与主导航同构）+ 主题纯图标，一行并排。折叠时都退成图标。 -->
    <div
      class="flex shrink-0 items-center gap-1 border-t border-line-soft p-2"
      :class="collapsed ? 'flex-col' : ''"
    >
      <RouterLink
        :to="SETTINGS_DESTINATION.path"
        custom
        v-slot="{ navigate, isActive }"
        class="min-w-0"
        :class="collapsed ? 'w-full' : 'flex-1'"
      >
        <button
          type="button"
          class="relative flex w-full items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-[13px] transition-colors"
          :class="[
            collapsed ? 'justify-center' : '',
            isActive
              ? 'bg-brand-soft font-medium text-brand-dark'
              : 'text-ink-soft hover:bg-line-soft hover:text-ink',
          ]"
          :title="collapsed ? t(SETTINGS_DESTINATION.labelKey) : undefined"
          @click="navigate"
        >
          <component :is="SETTINGS_DESTINATION.icon" :size="17" :stroke-width="1.9" class="shrink-0" />
          <span v-if="!collapsed" class="flex-1 truncate text-left">
            {{ t(SETTINGS_DESTINATION.labelKey) }}
          </span>
          <!-- 更新小圆点：只作提示，点击行为不变（仍跳设置）。有新版/已下载待重启时亮。 -->
          <span
            v-if="updateBadge"
            class="absolute right-1.5 top-1.5 size-2 rounded-full bg-brand ring-2 ring-surface"
            :title="updateBadgeTitle"
          />
        </button>
      </RouterLink>

      <button
        type="button"
        class="grid size-9 shrink-0 place-items-center rounded-[9px] text-ink-soft transition-colors hover:bg-line-soft hover:text-ink"
        :title="themeTitle"
        :aria-label="themeTitle"
        @click="toggleTheme"
      >
        <Moon v-if="isDark" :size="16" :stroke-width="1.9" />
        <Sun v-else :size="16" :stroke-width="1.9" />
      </button>
    </div>
  </aside>
</template>

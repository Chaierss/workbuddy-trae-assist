<script setup lang="ts">
import { ref, type Component } from "vue";
import { Monitor, Moon, Sun } from "lucide-vue-next";
import SectionCard from "../../components/SectionCard.vue";
import UpdateSection from "./UpdateSection.vue";
import { isDesktop } from "../../lib/desktop";
import { LOCALES, locale, setLocale, t, type MessageKey } from "../../lib/i18n";
import { restartOnboarding } from "../../lib/onboarding";
import { setTheme, theme, type ThemeMode } from "../../stores/app";

/**
 * 设置 → 外观与语言：主题、界面语言、版本更新（仅桌面端）与新手引导重开。
 * 这些都属于「看起来怎样」，与账号、调度、数据无关，单独成一栏。
 */
interface ThemeItem {
  key: ThemeMode;
  titleKey: MessageKey;
  descKey: MessageKey;
  icon: Component;
}

const themeItems: ThemeItem[] = [
  { key: "light", titleKey: "theme.light", descKey: "theme.light.desc", icon: Sun },
  { key: "dark", titleKey: "theme.dark", descKey: "theme.dark.desc", icon: Moon },
  { key: "system", titleKey: "theme.system", descKey: "theme.system.desc", icon: Monitor },
];

/* ---------- 新手引导：重新显示 ---------- */

const restarting = ref(false);
const restarted = ref(false);

async function restartGuide(): Promise<void> {
  restarting.value = true;
  try {
    await restartOnboarding();
    restarted.value = true;
  } finally {
    restarting.value = false;
  }
}
</script>

<template>
  <div class="space-y-4">
    <!-- 桌面端才有更新能力；浏览器里打开时整块不渲染 -->
    <UpdateSection v-if="isDesktop" />

    <SectionCard :title="t('theme.title')" :subtitle="t('theme.subtitle')">
      <div class="grid grid-cols-3 gap-2.5">
        <button
          v-for="item in themeItems"
          :key="item.key"
          type="button"
          class="flex flex-col items-start gap-2 rounded-control border p-3 text-left transition-colors"
          :class="
            theme === item.key
              ? 'border-brand bg-brand-soft'
              : 'border-line hover:border-line-strong dark:hover:border-line'
          "
          @click="setTheme(item.key)"
        >
          <span
            class="grid size-8 place-items-center rounded-control"
            :class="theme === item.key ? 'bg-brand text-white' : 'bg-line-soft text-ink-soft'"
          >
            <component :is="item.icon" :size="16" :stroke-width="1.9" />
          </span>
          <span class="text-body font-medium text-ink">{{ t(item.titleKey) }}</span>
          <span class="text-caption leading-relaxed text-muted">{{ t(item.descKey) }}</span>
        </button>
      </div>

      <div class="mt-4 flex items-center gap-3 border-t border-line-soft pt-3.5">
        <div class="min-w-0 flex-1">
          <div class="text-body font-medium text-ink">{{ t("theme.language") }}</div>
          <div class="mt-0.5 text-body-sm text-muted">{{ t("theme.language.desc") }}</div>
        </div>
        <div class="flex shrink-0 gap-1 rounded-control border border-line p-1">
          <button
            v-for="item in LOCALES"
            :key="item.code"
            type="button"
            class="rounded-badge px-2.5 py-1 text-body-sm transition-colors"
            :class="
              locale === item.code
                ? 'bg-brand text-white'
                : 'text-ink-soft hover:bg-line-soft hover:text-ink'
            "
            @click="setLocale(item.code)"
          >
            {{ item.label }}
          </button>
        </div>
      </div>
    </SectionCard>

    <SectionCard :title="t('onboarding.restart.title')" :subtitle="t('onboarding.restart.desc')">
      <div class="flex flex-wrap items-center gap-3">
        <button
          type="button"
          class="rounded-control border border-line px-3 py-1.5 text-caption text-ink-soft transition-colors hover:border-line-strong hover:text-ink disabled:opacity-60"
          :disabled="restarting"
          @click="restartGuide"
        >
          {{ t("onboarding.restart.cta") }}
        </button>
        <span v-if="restarted" class="text-caption text-muted">
          {{ t("onboarding.restart.done") }}
        </span>
      </div>
    </SectionCard>
  </div>
</template>

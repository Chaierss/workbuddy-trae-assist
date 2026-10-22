<script setup lang="ts">
import { onMounted, ref, type Component } from "vue";
import {
  Brain,
  Cable,
  IdCard,
  LayoutGrid,
  MessagesSquare,
  Monitor,
  Moon,
  Sun,
} from "lucide-vue-next";
import SectionCard from "../../components/SectionCard.vue";
import ToggleSwitch from "../../components/ToggleSwitch.vue";
import UpdateSection from "./UpdateSection.vue";
import { api, type Preferences } from "../../api";
import { isDesktop } from "../../lib/desktop";
import { LOCALES, locale, setLocale, t, type MessageKey } from "../../lib/i18n";
import { setTheme, theme, type ThemeMode } from "../../stores/app";

/**
 * 设置 → 常规：版本与更新（仅桌面端）+ 外观与语言 + 切换时的数据迁移范围。
 *
 * 「版本与更新」原先是一个独立分栏，收到这里是因为更新一年点不了几次，
 * 但「我现在是什么版本」随时会看，独立分栏反而多占一格注意力。
 *
 * 迁移开关默认全部**开启**（记忆/连接器/会话/档案/工作区），关掉某一块后切换就
 * 不再复制该块，值持久化在数据目录的 preferences.json。外观与语言存浏览器本地。
 *
 * 「账号保活 / 自动签到」原先也在这里，但那是「设置 → 计划任务」里同名任务的第二
 * 套开关，已统一收敛到计划任务页，本页不再重复提供入口。
 */
type PrefKey =
  | "migrateMemory"
  | "migrateConnectors"
  | "migrateSessions"
  | "migrateProfile"
  | "migrateWorkspaces";

interface ToggleItem {
  key: PrefKey;
  titleKey: MessageKey;
  descKey: MessageKey;
  icon: Component;
}

const migrateItems: ToggleItem[] = [
  { key: "migrateMemory", titleKey: "migrate.memory.title", descKey: "migrate.memory.desc", icon: Brain },
  { key: "migrateConnectors", titleKey: "migrate.connectors.title", descKey: "migrate.connectors.desc", icon: Cable },
  { key: "migrateSessions", titleKey: "migrate.sessions.title", descKey: "migrate.sessions.desc", icon: MessagesSquare },
  { key: "migrateProfile", titleKey: "migrate.profile.title", descKey: "migrate.profile.desc", icon: IdCard },
  { key: "migrateWorkspaces", titleKey: "migrate.workspaces.title", descKey: "migrate.workspaces.desc", icon: LayoutGrid },
];

const prefs = ref<Preferences | null>(null);
const saving = ref<string | null>(null);

/** 外观三选一。主题只影响界面配色，不进 preferences.json，存在浏览器本地。 */
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

onMounted(async () => {
  try {
    prefs.value = await api.preferences();
  } catch {
    prefs.value = null;
  }
});

async function toggle(key: PrefKey): Promise<void> {
  if (!prefs.value) return;
  saving.value = key;
  try {
    prefs.value = await api.savePreferences({ [key]: !prefs.value[key] });
  } finally {
    saving.value = null;
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
          class="flex flex-col items-start gap-2 rounded-[10px] border p-3 text-left transition-colors"
          :class="
            theme === item.key
              ? 'border-brand bg-brand-soft'
              : 'border-line hover:border-[#d5d8d3] dark:hover:border-line'
          "
          @click="setTheme(item.key)"
        >
          <span
            class="grid size-8 place-items-center rounded-[9px]"
            :class="theme === item.key ? 'bg-brand text-white' : 'bg-line-soft text-ink-soft'"
          >
            <component :is="item.icon" :size="16" :stroke-width="1.9" />
          </span>
          <span class="text-[13px] font-medium text-ink">{{ t(item.titleKey) }}</span>
          <span class="text-[11.5px] leading-relaxed text-muted">{{ t(item.descKey) }}</span>
        </button>
      </div>

      <div class="mt-4 flex items-center gap-3 border-t border-line-soft pt-3.5">
        <div class="min-w-0 flex-1">
          <div class="text-[13px] font-medium text-ink">{{ t("theme.language") }}</div>
          <div class="mt-0.5 text-[12px] text-muted">{{ t("theme.language.desc") }}</div>
        </div>
        <div class="flex shrink-0 gap-1 rounded-[9px] border border-line p-1">
          <button
            v-for="item in LOCALES"
            :key="item.code"
            type="button"
            class="rounded-[6px] px-2.5 py-1 text-[12px] transition-colors"
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

    <SectionCard :title="t('migrate.title')" :subtitle="t('migrate.subtitle')">
      <div class="divide-y divide-line-soft">
        <div v-for="item in migrateItems" :key="item.key" class="flex items-center gap-3 py-3.5">
          <span class="grid size-9 shrink-0 place-items-center rounded-[10px] bg-brand-soft text-brand-dark">
            <component :is="item.icon" :size="17" :stroke-width="1.9" />
          </span>
          <div class="min-w-0 flex-1">
            <div class="text-[13px] font-medium text-ink">{{ t(item.titleKey) }}</div>
            <div class="mt-0.5 text-[12px] text-muted">{{ t(item.descKey) }}</div>
          </div>
          <ToggleSwitch
            :model-value="!!prefs?.[item.key]"
            :disabled="saving === item.key"
            @update:model-value="() => toggle(item.key)"
          />
        </div>
      </div>
    </SectionCard>

    <p class="text-[12px] leading-relaxed text-muted">{{ t("migrate.footnote") }}</p>
  </div>
</template>

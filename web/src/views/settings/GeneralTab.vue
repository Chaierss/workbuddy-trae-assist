<script setup lang="ts">
import { computed, onMounted, ref, type Component } from "vue";
import {
  Brain,
  Cable,
  Globe,
  IdCard,
  LayoutGrid,
  MessagesSquare,
  Monitor,
  Moon,
  Repeat2,
  Sun,
} from "lucide-vue-next";
import SectionCard from "../../components/SectionCard.vue";
import ToggleSwitch from "../../components/ToggleSwitch.vue";
import UpdateSection from "./UpdateSection.vue";
import { api, type LoginStateDiagnostics, type Preferences } from "../../api";
import { isDesktop } from "../../lib/desktop";
import { LOCALES, locale, setLocale, t, type MessageKey } from "../../lib/i18n";
import { restartOnboarding } from "../../lib/onboarding";
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
  | "migrateWorkspaces"
  | "migrateWebview"
  | "relaunchAfterSwitch";

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
  // 只对 WorkBuddy 生效：网页版登录态存在客户端的内嵌浏览器分区里，
  // 不换过来的话切号后网页版仍然是上一个号（或直接未登录）。
  { key: "migrateWebview", titleKey: "migrate.webview.title", descKey: "migrate.webview.desc", icon: Globe },
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

/* ---------- 网页版登录态诊断（只读） ---------- */

/**
 * 客户端把网页版登录态存在按账号 uid 命名的 Chromium 分区里。没分区的号
 * 切过去之后网页版必然是未登录 —— 以前这件事没有任何地方能看出来，
 * 用户只会觉得「切号把网页版弄坏了」。这里把归属关系摊开，只读、不修。
 */
const diagnostics = ref<LoginStateDiagnostics | null>(null);
const diagnosticsError = ref("");
const diagnosticsLoading = ref(false);

async function loadDiagnostics(): Promise<void> {
  diagnosticsLoading.value = true;
  diagnosticsError.value = "";
  try {
    diagnostics.value = await api.loginStateDiagnostics();
  } catch (error) {
    diagnosticsError.value = (error as Error).message;
    diagnostics.value = null;
  } finally {
    diagnosticsLoading.value = false;
  }
}

/** 只看 WorkBuddy：网页版登录态是它的能力，Trae 没有这套分区。 */
const webviewAccounts = computed(() =>
  (diagnostics.value?.accounts ?? []).filter((item) => item.scope.startsWith("workbuddy")),
);
const webviewCovered = computed(
  () => webviewAccounts.value.filter((item) => item.hasWebviewPartition).length,
);
const webviewMissing = computed(() =>
  webviewAccounts.value.filter((item) => !item.hasWebviewPartition),
);

onMounted(async () => {
  try {
    prefs.value = await api.preferences();
  } catch {
    prefs.value = null;
  }
  void loadDiagnostics();
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

/** 供给阈值：改完即存；存不下就重新拉一次，避免界面停在假值上。 */
const thresholdBusy = ref<string | null>(null);

async function saveThreshold(
  field: "minHealthyAccounts" | "minCreditsDays",
  raw: string,
): Promise<void> {
  const value = Number(raw);
  if (!Number.isFinite(value) || value < 0) return;
  thresholdBusy.value = field;
  try {
    prefs.value = await api.savePreferences({ [field]: value });
  } catch {
    try {
      prefs.value = await api.preferences();
    } catch {
      // 读不到就保持原值：阈值只是告警参数，不值得让页面报错。
    }
  } finally {
    thresholdBusy.value = null;
  }
}
</script>

<template>
  <div class="space-y-4">
    <!-- 桌面端才有更新能力；浏览器里打开时整块不渲染 -->
    <UpdateSection v-if="isDesktop" />

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

    <SectionCard :title="t('migrate.title')" :subtitle="t('migrate.subtitle')">
      <div class="divide-y divide-line-soft">
        <div v-for="item in migrateItems" :key="item.key" class="flex items-center gap-3 py-3.5">
          <span class="grid size-9 shrink-0 place-items-center rounded-control bg-brand-soft text-brand-dark">
            <component :is="item.icon" :size="17" :stroke-width="1.9" />
          </span>
          <div class="min-w-0 flex-1">
            <div class="text-body font-medium text-ink">{{ t(item.titleKey) }}</div>
            <div class="mt-0.5 text-body-sm text-muted">{{ t(item.descKey) }}</div>
          </div>
          <ToggleSwitch
            :model-value="!!prefs?.[item.key]"
            :disabled="saving === item.key"
            @update:model-value="() => toggle(item.key)"
          />
        </div>
      </div>
    </SectionCard>

    <!-- 号池供给：低于阈值时概览页会出现「号池供给不足」告警 -->
    <SectionCard :title="t('settings.supply.title')" :subtitle="t('settings.supply.desc')">
      <div class="grid gap-3 sm:grid-cols-2">
        <label class="flex items-center justify-between gap-3 rounded-control border border-line-soft px-3 py-2.5">
          <span class="min-w-0 text-body-sm text-ink-soft">
            {{ t("settings.supply.minHealthy") }}
          </span>
          <input
            type="number"
            min="0"
            step="1"
            class="w-[68px] shrink-0 rounded-md border border-line-soft bg-surface px-1.5 py-0.5 text-right text-body-sm tabular-nums text-ink outline-none"
            :value="prefs?.minHealthyAccounts ?? 2"
            :disabled="thresholdBusy === 'minHealthyAccounts'"
            @change="saveThreshold('minHealthyAccounts', ($event.target as HTMLInputElement).value)"
          />
        </label>
        <label class="flex items-center justify-between gap-3 rounded-control border border-line-soft px-3 py-2.5">
          <span class="min-w-0 text-body-sm text-ink-soft">
            {{ t("settings.supply.minCreditsDays") }}
          </span>
          <input
            type="number"
            min="0"
            step="0.5"
            class="w-[68px] shrink-0 rounded-md border border-line-soft bg-surface px-1.5 py-0.5 text-right text-body-sm tabular-nums text-ink outline-none"
            :value="prefs?.minCreditsDays ?? 3"
            :disabled="thresholdBusy === 'minCreditsDays'"
            @change="saveThreshold('minCreditsDays', ($event.target as HTMLInputElement).value)"
          />
        </label>
      </div>
      <p class="mt-2 text-caption text-muted">{{ t("settings.supply.hint") }}</p>
    </SectionCard>

    <!-- 换号体验：切换必须让客户端退出一次，这里决定切完是否自动拉起 -->
    <SectionCard
      :title="t('settings.general.switchTitle')"
      :subtitle="t('settings.general.switchHint')"
    >
      <div class="flex items-center gap-3">
        <span
          class="grid size-9 shrink-0 place-items-center rounded-control bg-brand-soft text-brand-dark"
        >
          <Repeat2 :size="17" :stroke-width="1.9" />
        </span>
        <div class="min-w-0 flex-1">
          <div class="text-body font-medium text-ink">
            {{ t("settings.general.relaunchAfterSwitch") }}
          </div>
          <div class="mt-0.5 text-body-sm text-muted">
            {{ t("settings.general.relaunchAfterSwitchDesc") }}
          </div>
        </div>
        <ToggleSwitch
          :model-value="!!prefs?.relaunchAfterSwitch"
          :disabled="saving === 'relaunchAfterSwitch'"
          @update:model-value="() => toggle('relaunchAfterSwitch')"
        />
      </div>
    </SectionCard>

    <!-- 网页版登录态诊断：只读摊开归属关系，回答「为什么切完网页版还得登录」 -->
    <SectionCard :title="t('webview.title')" :subtitle="t('webview.subtitle')">
      <div class="flex items-center justify-between gap-3">
        <p class="text-body-sm leading-relaxed text-ink-soft">
          {{
            diagnostics
              ? t("webview.summary", {
                  partitions: diagnostics.webview.partitionCount,
                  covered: webviewCovered,
                  total: webviewAccounts.length,
                })
              : diagnosticsLoading
                ? t("webview.loading")
                : t("webview.unavailable")
          }}
        </p>
        <button
          type="button"
          class="shrink-0 rounded-control border border-line bg-surface px-2.5 py-1.5 text-body-sm text-ink-soft transition-colors hover:border-line-strong hover:text-ink disabled:opacity-60"
          :disabled="diagnosticsLoading"
          @click="loadDiagnostics"
        >
          {{ t("webview.refresh") }}
        </button>
      </div>

      <p v-if="diagnosticsError" class="mt-2 text-body-sm text-danger">
        {{ t("webview.error", { reason: diagnosticsError }) }}
      </p>

      <div v-else-if="webviewMissing.length" class="mt-2.5">
        <p class="text-body-sm text-warn">
          {{ t("webview.missingHint", { count: webviewMissing.length }) }}
        </p>
        <div class="mt-1.5 flex flex-wrap gap-1.5">
          <span
            v-for="item in webviewMissing"
            :key="item.accountId"
            class="rounded-full bg-warn-soft/70 px-2 py-0.5 text-caption text-warn"
          >
            {{ item.displayName }}
          </span>
        </div>
      </div>

      <p v-else-if="diagnostics && webviewAccounts.length" class="mt-2 text-body-sm text-muted">
        {{ t("webview.allCovered") }}
      </p>

      <p v-if="diagnostics" class="mt-2.5 break-all font-mono text-caption leading-relaxed text-muted">
        {{ diagnostics.partitionsDir }}
      </p>
    </SectionCard>

    <p class="text-body-sm leading-relaxed text-muted">{{ t("migrate.footnote") }}</p>
  </div>
</template>

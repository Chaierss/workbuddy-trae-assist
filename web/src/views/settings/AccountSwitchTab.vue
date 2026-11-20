<script setup lang="ts">
import { computed, onMounted, ref, type Component } from "vue";
import {
  Brain,
  Cable,
  Globe,
  IdCard,
  LayoutGrid,
  MessagesSquare,
  Repeat2,
} from "lucide-vue-next";
import SectionCard from "../../components/SectionCard.vue";
import ToggleSwitch from "../../components/ToggleSwitch.vue";
import SyncTab from "./SyncTab.vue";
import { api, type LoginStateDiagnostics, type Preferences } from "../../api";
import { t, type MessageKey } from "../../lib/i18n";

/**
 * 设置 → 账号与切换：切换后行为、切换时迁移哪些数据、网页版登录态归属，
 * 以及登录态同步。都属于「换号时会发生什么」，此前散落在「常规」和「同步」两栏。
 */
type PrefKey = "migrateMemory" | "migrateConnectors" | "migrateSessions" | "migrateProfile" | "migrateWorkspaces" | "migrateWebview" | "relaunchAfterSwitch";

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

/* ---------- 网页版登录态诊断（只读） ---------- */

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
</script>

<template>
  <div class="space-y-4">
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
      <p class="mt-3 text-body-sm leading-relaxed text-muted">{{ t("migrate.footnote") }}</p>
    </SectionCard>

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

    <SyncTab />
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, type Component } from "vue";
import { RefreshCw, ShieldCheck } from "lucide-vue-next";
import SectionCard from "../../components/SectionCard.vue";
import ToggleSwitch from "../../components/ToggleSwitch.vue";
import ScheduleTab from "./ScheduleTab.vue";
import { api, type Preferences } from "../../api";
import { t, type MessageKey } from "../../lib/i18n";

/**
 * 设置 → 自动化：计划任务、自动轮换、健康中心自动处置、号池供给阈值与网关隔离。
 * 这一页回答的是「什么会自己跑、跑不动时怎么告警」，所有可写入口都收在这里，
 * 计划任务页与开关页不再各写一份。
 */
type PrefKey = "healthAutoRefresh" | "healthAutoRerun" | "isolationEnabled";

interface ToggleItem {
  key: PrefKey;
  titleKey: MessageKey;
  descKey: MessageKey;
  icon: Component;
}

/** 健康中心自动处置：默认开；两项都会实际打上游，用户显式关掉就不再执行。 */
const healthItems: ToggleItem[] = [
  {
    key: "healthAutoRefresh",
    titleKey: "settings.health.refreshTitle",
    descKey: "settings.health.refreshDesc",
    icon: RefreshCw,
  },
  {
    key: "healthAutoRerun",
    titleKey: "settings.health.rerunTitle",
    descKey: "settings.health.rerunDesc",
    icon: ShieldCheck,
  },
];

const prefs = ref<Preferences | null>(null);
const saving = ref<string | null>(null);

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

/** 供给阈值：改完即存；存不下就重新拉一次，避免界面停在假值上。 */
const thresholdBusy = ref<string | null>(null);

async function saveThreshold(
  field:
    | "minHealthyAccounts"
    | "minCreditsDays"
    | "isolationThreshold"
    | "isolationHours"
    | "activityDeadlineDays",
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
    <ScheduleTab />

    <!-- 健康中心自动处置：默认开，两件事都会实际打上游；关掉即完全不动 -->
    <SectionCard :title="t('settings.health.title')" :subtitle="t('settings.health.desc')">
      <div class="divide-y divide-line-soft">
        <div v-for="item in healthItems" :key="item.key" class="flex items-center gap-3 py-3.5">
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

    <!-- 网关自动隔离：只影响网关候选，不影响切号与数据 -->
    <SectionCard :title="t('settings.isolation.title')" :subtitle="t('settings.isolation.desc')">
      <div class="flex items-center gap-3 border-b border-line-soft pb-3.5">
        <span class="grid size-9 shrink-0 place-items-center rounded-control bg-brand-soft text-brand-dark">
          <ShieldCheck :size="17" :stroke-width="1.9" />
        </span>
        <div class="min-w-0 flex-1">
          <div class="text-body font-medium text-ink">{{ t("settings.isolation.toggle") }}</div>
          <div class="mt-0.5 text-body-sm text-muted">{{ t("settings.isolation.toggleDesc") }}</div>
        </div>
        <ToggleSwitch
          :model-value="!!prefs?.isolationEnabled"
          :disabled="saving === 'isolationEnabled'"
          @update:model-value="() => toggle('isolationEnabled')"
        />
      </div>
      <div class="mt-3 grid gap-3 sm:grid-cols-3">
        <label class="flex items-center justify-between gap-3 rounded-control border border-line-soft px-3 py-2.5">
          <span class="min-w-0 text-body-sm text-ink-soft">{{ t("settings.isolation.threshold") }}</span>
          <input
            type="number"
            min="1"
            max="10"
            step="1"
            class="w-[58px] shrink-0 rounded-md border border-line-soft bg-surface px-1.5 py-0.5 text-right text-body-sm tabular-nums text-ink outline-none"
            :value="prefs?.isolationThreshold ?? 3"
            :disabled="thresholdBusy === 'isolationThreshold'"
            @change="saveThreshold('isolationThreshold', ($event.target as HTMLInputElement).value)"
          />
        </label>
        <label class="flex items-center justify-between gap-3 rounded-control border border-line-soft px-3 py-2.5">
          <span class="min-w-0 text-body-sm text-ink-soft">{{ t("settings.isolation.hours") }}</span>
          <input
            type="number"
            min="1"
            max="168"
            step="1"
            class="w-[58px] shrink-0 rounded-md border border-line-soft bg-surface px-1.5 py-0.5 text-right text-body-sm tabular-nums text-ink outline-none"
            :value="prefs?.isolationHours ?? 24"
            :disabled="thresholdBusy === 'isolationHours'"
            @change="saveThreshold('isolationHours', ($event.target as HTMLInputElement).value)"
          />
        </label>
        <label class="flex items-center justify-between gap-3 rounded-control border border-line-soft px-3 py-2.5">
          <span class="min-w-0 text-body-sm text-ink-soft">{{ t("settings.isolation.deadlineDays") }}</span>
          <input
            type="number"
            min="1"
            max="14"
            step="1"
            class="w-[58px] shrink-0 rounded-md border border-line-soft bg-surface px-1.5 py-0.5 text-right text-body-sm tabular-nums text-ink outline-none"
            :value="prefs?.activityDeadlineDays ?? 3"
            :disabled="thresholdBusy === 'activityDeadlineDays'"
            @change="saveThreshold('activityDeadlineDays', ($event.target as HTMLInputElement).value)"
          />
        </label>
      </div>
      <p class="mt-2 text-caption text-muted">{{ t("settings.isolation.hint") }}</p>
    </SectionCard>
  </div>
</template>

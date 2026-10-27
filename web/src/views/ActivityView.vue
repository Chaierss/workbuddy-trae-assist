<script setup lang="ts">
/**
 * 活动页。
 *
 * 渲染顺序是刻意的：**先展示有哪些活动，再去检测**。
 *   1. 目录（`/activities/catalog`，零网络）+ 本地号池清单 → 立刻铺出骨架（「未检测」）；
 *   2. 再拉 `/activities` 逐个账号检测 → 按 key 覆盖成真实状态/进度/收益。
 * 这样冷启动不会先看到一片空白，用户也不会以为「没活动」。
 *
 * 布局按**活动**聚合，不按账号：后端是逐账号出条目的，一个号一张卡的结果是
 * 16 个账号 × 3 项活动 = 48 张几乎一样的卡片，真正的问题（「谁还没签」）被埋掉。
 * 现在每个活动一行，账号退化成状态网格，缺的人一眼可见。
 *
 * 三条纪律写死在界面上：
 *   1. 查不了就写「不可查」+ 原因，绝不显示成「没有活动」；
 *   2. 不可自动化的项必须写明理由，而不是悄悄不出现；
 *   3. 一键执行后如实汇报「成功 / 跳过 / 失败」，跳过也要给出原因。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch, type Component } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  CircleDashed,
  Clock,
  ExternalLink,
  Loader2,
  RefreshCw,
  Wand2,
} from "lucide-vue-next";
import ConfirmDialog from "../components/ConfirmDialog.vue";
import PageHeader from "../components/PageHeader.vue";
import SectionCard from "../components/SectionCard.vue";
import SegmentedControl from "../components/SegmentedControl.vue";
import SelectMenu from "../components/SelectMenu.vue";
import StatusPill from "../components/StatusPill.vue";
import OfficialActivitiesPanel from "../components/activity/OfficialActivitiesPanel.vue";
import ActivityRunsPanel from "../components/activity/ActivityRunsPanel.vue";
import { safeActivityUrl } from "../lib/activity";
import { acknowledgeGrowthNotice, growthNoticeAcknowledged } from "../lib/growthNotice";
import { t, type MessageKey } from "../lib/i18n";
import {
  api,
  type AccountActivities,
  type ActivityCampaign,
  type ActivityCatalog,
  type ActivityCatalogEntry,
  type ActivityItem,
  type ActivityRun,
  type ActivityRunReport,
  type ActivityState,
  type ActivitySummary,
} from "../api";

type Tab = "official" | "accounts" | "runs";

const route = useRoute();
const router = useRouter();

function readTab(raw: unknown): Tab {
  return raw === "accounts" || raw === "runs" ? raw : "official";
}

const tab = ref<Tab>(readTab(route.query.tab));

watch(
  () => route.query.tab,
  (value) => {
    tab.value = readTab(value);
  },
);

function setTab(next: Tab): void {
  tab.value = next;
  const query: Record<string, string> = { tab: next };
  if (next === "official" && typeof route.query.campaign === "string") {
    query.campaign = route.query.campaign;
  }
  void router.replace({ query });
}

const initialCampaign = computed(() =>
  typeof route.query.campaign === "string" ? route.query.campaign : null,
);

function selectCampaign(key: string | null): void {
  const query: Record<string, string> = { tab: "official" };
  if (key) query.campaign = key;
  void router.replace({ query });
}

const TAB_OPTIONS = computed(() => [
  { value: "official", label: t("activity.tabOfficial") },
  { value: "accounts", label: t("activity.tabAccounts") },
  { value: "runs", label: t("activity.tabRuns") },
]);

/** 状态比检测状态多一档「未检测」——目录已铺出、检测还没回来。 */
type CardState = ActivityState | "undetected";

const STATE_TEXT: Record<CardState, MessageKey> = {
  done: "activity.stateDone",
  todo: "activity.stateTodo",
  in_progress: "activity.stateInProgress",
  not_started: "activity.stateNotStarted",
  unavailable: "activity.stateUnavailable",
  undetected: "activity.stateUndetected",
};

const STATE_ICON: Record<CardState, Component> = {
  done: CheckCircle2,
  todo: CircleDashed,
  in_progress: Clock,
  not_started: CircleDashed,
  unavailable: AlertTriangle,
  undetected: CircleDashed,
};

/** 账号状态块：颜色即状态，不额外占一行文字。 */
const CHIP_CLASS: Record<CardState, string> = {
  done: "border-brand/25 bg-brand-soft text-brand-dark",
  todo: "border-warn/30 bg-warn-soft text-warn",
  in_progress: "border-info/30 bg-info-soft text-info",
  not_started: "border-line bg-line-soft text-muted",
  unavailable: "border-danger/25 bg-danger-soft text-danger",
  undetected: "border-line bg-line-soft text-muted",
};

const BAR_CLASS: Record<CardState, string> = {
  done: "bg-brand",
  todo: "bg-warn",
  in_progress: "bg-info",
  not_started: "bg-line",
  unavailable: "bg-danger",
  undetected: "bg-line",
};

/** 状态排序：待办的排前面 —— 这一屏是给人「找该做什么」的。 */
const STATE_ORDER: Record<CardState, number> = {
  todo: 0,
  in_progress: 1,
  not_started: 2,
  unavailable: 3,
  undetected: 4,
  done: 5,
};

const GROUP_LABEL: Record<string, MessageKey> = {
  daily: "activity.groupDaily",
  seasonal: "activity.groupSeasonal",
};

const PLATFORM_LABEL: Record<string, string> = {
  workbuddy: "WorkBuddy",
  trae: "Trae",
};

function groupLabel(group: string): string {
  return GROUP_LABEL[group] ? t(GROUP_LABEL[group]) : group;
}

/** 一个账号在某项活动上的状态。 */
interface AccountEntry {
  accountId: string;
  name: string;
  state: CardState;
  actionable: boolean;
  percent: number;
  progressText: string;
  note: string;
  manualReason: string;
  manualTasks: Array<{ code: string; title: string; desc: string; jumpUrl: string }>;
  jumpUrl: string;
  rewardText: string;
  rewardHint: string;
  period: string;
  detail: string;
}

/** 一项活动 + 它在各账号上的状态。 */
interface ActivityGroupView {
  key: string;
  title: string;
  subtitle: string;
  description: string;
  group: string;
  platform: string;
  automatable: boolean;
  entries: AccountEntry[];
  done: number;
  todo: number;
  manual: number;
  unavailable: number;
  undetected: number;
}

interface AccountBase {
  id: string;
  displayName: string;
  platform: string;
  scope: string;
}

const catalog = ref<ActivityCatalogEntry[]>([]);
const catalogMeta = ref<ActivityCatalog | null>(null);
const knownAccounts = ref<AccountBase[]>([]);
const pool = ref<AccountActivities[]>([]);
const summary = ref<ActivitySummary | null>(null);
const campaigns = ref<ActivityCampaign[]>([]);
const detectedAt = ref<string | null>(null);
const busy = ref(false);
const running = ref(false);
/** 正在执行的活动 key（null = 全量或按账号执行）。 */
const runningKeys = ref<string[] | null>(null);
const error = ref<string | null>(null);
const platformFilter = ref<"all" | "workbuddy" | "trae">("all");
const hideDone = ref(false);
const runReport = ref<ActivityRunReport | null>(null);
/** 最近执行流水：落库的是每轮最终结论，用来回看「为什么失败」。 */
const runs = ref<ActivityRun[]>([]);
const runningTargets = ref<Set<string>>(new Set());
const expanded = ref<Set<string>>(new Set());
const toast = ref<string | null>(null);

/** 官方面板当前执行的活动 key；按账号执行 / 全量执行时为 null。 */
const activeRunningKey = computed(() =>
  runningKeys.value?.length === 1 ? runningKeys.value[0] : null,
);

function percentOf(state: CardState, current: number | null, target: number | null): number {
  if (state === "done") return 100;
  if (state !== "todo" && state !== "in_progress") return 0;
  if (current !== null && target !== null && target > 0) {
    return Math.max(0, Math.min(100, Math.round((current / target) * 100)));
  }
  return state === "in_progress" ? 50 : 0;
}

function periodOf(start: string | null, end: string | null): string {
  const from = start?.slice(0, 10);
  const to = end?.slice(0, 10);
  if (!from && !to) return "";
  return `${from ?? "?"} ~ ${to ?? "?"}`;
}

/** 目录项（未检测）与检测结果合成同一个 entry 形态。 */
function entryOf(
  account: AccountBase,
  key: string,
  item: ActivityItem | undefined,
  entry: ActivityCatalogEntry | undefined,
): AccountEntry {
  if (!item) {
    return {
      accountId: account.id,
      name: account.displayName,
      state: "undetected",
      actionable: false,
      percent: 0,
      progressText: "",
      note: "",
      manualReason: "",
      manualTasks: [],
      jumpUrl: "",
      rewardText: "",
      rewardHint: entry?.rewardHint ?? "",
      period: "",
      detail: entry?.description ?? "",
    };
  }
  return {
    accountId: account.id,
    name: account.displayName,
    state: item.state,
    actionable: item.automatable && item.state === "todo",
    percent: percentOf(item.state, item.progressCurrent, item.progressTarget),
    progressText: item.progressText,
    note: item.note,
    manualReason: item.manualReason,
    manualTasks: (item.manualTasks ?? []).map((task) => ({
      ...task,
      jumpUrl: safeActivityUrl(task.jumpUrl) ?? "",
    })),
    jumpUrl: safeActivityUrl(item.jumpUrl) ?? "",
    rewardText: item.rewardText,
    rewardHint: entry?.rewardHint ?? "",
    period: periodOf(item.periodStart, item.periodEnd),
    detail: entry?.description ?? "",
  };
}

/** 号池清单的兜底：账号接口失败但检测成功时，用检测结果里的账号补上，避免整页空白。 */
const accountBases = computed<AccountBase[]>(() =>
  knownAccounts.value.length
    ? knownAccounts.value
    : pool.value.map((item) => ({
        id: item.accountId,
        displayName: item.displayName,
        platform: item.platform,
        scope: item.scope,
      })),
);

/** 账号 × 活动 → 活动 × 账号：按活动 key 聚合，缺项用目录骨架补齐。 */
const activityGroups = computed<ActivityGroupView[]>(() => {
  const detected = new Map(pool.value.map((item) => [item.accountId, item]));
  const byKey = new Map<string, ActivityGroupView>();

  const ensure = (key: string, platform: string, item?: ActivityItem, entry?: ActivityCatalogEntry) => {
    const found = byKey.get(key);
    if (found) return found;
    const created: ActivityGroupView = {
      key,
      title: entry?.title ?? item?.title ?? key,
      subtitle: entry?.subtitle ?? item?.subtitle ?? "",
      description: entry?.description ?? "",
      group: entry?.group ?? item?.group ?? "daily",
      platform: entry?.platform ?? platform,
      automatable: entry?.automatable ?? item?.automatable ?? false,
      entries: [],
      done: 0,
      todo: 0,
      manual: 0,
      unavailable: 0,
      undetected: 0,
    };
    byKey.set(key, created);
    return created;
  };

  for (const base of accountBases.value) {
    const account = detected.get(base.id);
    const items = account?.activities ?? [];
    const seen = new Set<string>();

    for (const item of items) {
      const entry = catalog.value.find((row) => row.key === item.key);
      ensure(item.key, base.platform, item, entry);
      seen.add(item.key);
    }
    // 该平台目录里、这个账号还没有结果的项，补成「未检测」而不是消失。
    for (const row of catalog.value) {
      if (row.platform !== base.platform || seen.has(row.key)) continue;
      ensure(row.key, base.platform, undefined, row);
    }
    // 统一挂 entry：先把该账号所有相关 key 收集齐，再一次性写进分组。
    for (const [key, group] of byKey) {
      const item = items.find((row) => row.key === key);
      const entry = catalog.value.find((row) => row.key === key);
      if (!item && entry?.platform !== base.platform) continue;
      group.entries.push(entryOf(base, key, item, entry));
    }
  }

  const list = [...byKey.values()];
  for (const group of list) {
    group.entries.sort(
      (a, b) => STATE_ORDER[a.state] - STATE_ORDER[b.state] || a.name.localeCompare(b.name),
    );
    const visible = hideDone.value
      ? group.entries.filter((entry) => entry.state !== "done")
      : group.entries;
    group.entries = visible;
    group.done = visible.filter((entry) => entry.state === "done").length;
    group.todo = visible.filter((entry) => entry.actionable).length;
    group.manual = visible.filter(
      (entry) => !entry.actionable && entry.state !== "done" && entry.state !== "unavailable",
    ).length;
    group.unavailable = visible.filter((entry) => entry.state === "unavailable").length;
    group.undetected = visible.filter((entry) => entry.state === "undetected").length;
  }

  return list
    .filter((group) => group.entries.length > 0)
    .filter((group) => platformFilter.value === "all" || group.platform === platformFilter.value)
    .sort((a, b) => {
      // 有可执行项的排前面；其次按待办多寡；最后按标题稳定排序。
      if ((b.todo > 0 ? 1 : 0) !== (a.todo > 0 ? 1 : 0)) return b.todo > 0 ? 1 : -1;
      if (b.todo !== a.todo) return b.todo - a.todo;
      return a.title.localeCompare(b.title);
    });
});

const visibleAccounts = computed(() =>
  accountBases.value.filter(
    (account) => platformFilter.value === "all" || account.platform === platformFilter.value,
  ),
);

const actionable = computed(() => summary.value?.actionable ?? 0);
const canRun = computed(() => actionable.value > 0 && !running.value && !busy.value);
const detecting = computed(() => busy.value && !pool.value.length);
const totalEntries = computed(() =>
  activityGroups.value.reduce((sum, group) => sum + group.entries.length, 0),
);

function showToast(message: string): void {
  toast.value = message;
  window.clearTimeout((showToast as unknown as { timer?: number }).timer);
  (showToast as unknown as { timer?: number }).timer = window.setTimeout(() => {
    toast.value = null;
  }, 2600);
}

async function load(refresh = false): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const [catalogResult, accountsResult] = await Promise.all([
      api.activityCatalog(),
      api.mergedAccounts(),
    ]);
    catalog.value = catalogResult.entries;
    catalogMeta.value = catalogResult;
    knownAccounts.value = accountsResult.accounts.map((account) => ({
      id: account.id,
      displayName: account.displayName,
      platform: account.platform,
      scope: account.scope,
    }));

    const data = await api.activities(refresh);
    pool.value = data.accounts;
    summary.value = data.summary;
    campaigns.value = data.campaigns ?? [];
    detectedAt.value = data.detectedAt;
    void loadRuns();
  } catch (cause) {
    error.value = (cause as Error).message;
  } finally {
    busy.value = false;
  }
}

async function loadRuns(): Promise<void> {
  try {
    const data = await api.activityRuns(7, 200);
    runs.value = data.runs;
  } catch {
    // 流水只是辅助信息，读不到不影响活动页主流程。
    runs.value = [];
  }
}

/**
 * 首次点「一键执行」前的风险确认。
 *
 * 成长任务里有 10 项是**用接口把任务标记为完成**，不是你客户端里真实做了那个动作，
 * 所以第一次跑之前必须让用户知道这条（文案见 `activity.noticeBody`）。
 * ack 标记与概览页共用，见 `lib/growthNotice`。
 */
interface PendingRun {
  accountIds: string[];
  keys: string[] | null;
}

const pendingRun = ref<PendingRun | null>(null);

/**
 * @param accountIds 限定账号；省略 = 按当前视图推导。
 * @param keys 限定活动范围；省略 = 全部活动。
 */
async function execute(accountIds?: string[], keys?: string[]): Promise<void> {
  if (running.value) return;
  const targets = accountIds?.length
    ? accountIds
    : keys?.length
      ? []
      : activityGroups.value.flatMap((group) =>
          group.entries.filter((entry) => entry.actionable).map((entry) => entry.accountId),
        );
  if (!targets.length && !keys?.length) {
    showToast(t("activity.toastNothingToRun"));
    return;
  }
  const unique = Array.from(new Set(targets));
  if (!growthNoticeAcknowledged()) {
    pendingRun.value = { accountIds: unique, keys: keys ?? null };
    return;
  }
  await runTargets(unique, keys);
}

function cancelNotice(): void {
  pendingRun.value = null;
}

async function confirmNotice(): Promise<void> {
  const next = pendingRun.value;
  pendingRun.value = null;
  if (!next) return;
  acknowledgeGrowthNotice();
  await runTargets(next.accountIds, next.keys ?? undefined);
}

async function runTargets(targets: string[], keys?: string[]): Promise<void> {
  running.value = true;
  runningKeys.value = keys ?? null;
  runningTargets.value = new Set(targets);
  try {
    const report = await api.runActivities(
      targets.length ? targets : undefined,
      keys?.length ? keys : undefined,
    );
    runReport.value = report;
    const { ok, skipped, failed } = report.summary;
    if (!report.summary.total) {
      showToast(t("activity.toastNothingToRun"));
    } else {
      showToast(t("activity.toastRunDone", { ok, skipped, failed }));
    }
    await load(true);
  } catch (cause) {
    showToast((cause as Error).message);
  } finally {
    running.value = false;
    runningKeys.value = null;
    runningTargets.value = new Set();
  }
}

function toggleDetail(key: string): void {
  const next = new Set(expanded.value);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  expanded.value = next;
}

function completionText(group: ActivityGroupView): string {
  const total = group.entries.length;
  const done = group.entries.filter((entry) => entry.state === "done").length;
  return t("activity.doneRatio", { done, total });
}

/** 已完成占比：通栏色带容易被读成「分隔线」，改成一枚小条贴在数字下面。 */
function donePercent(group: ActivityGroupView): number {
  const total = group.entries.length;
  if (!total) return 0;
  return Math.round((group.entries.filter((entry) => entry.state === "done").length / total) * 100);
}

// ---------- 自动刷新 ----------

const REFRESH_KEY = "chaierww.activity-refresh-seconds";
const refreshSeconds = ref(Number(localStorage.getItem(REFRESH_KEY) ?? 60));
const refreshOptions = computed(() => [
  { value: 0, label: t("accounts.refreshOff") },
  { value: 30, label: t("accounts.refresh30") },
  { value: 60, label: t("accounts.refresh60") },
  { value: 300, label: t("accounts.refresh300") },
]);

let refreshTimer: number | null = null;

function applyRefreshTimer(): void {
  if (refreshTimer !== null) {
    window.clearInterval(refreshTimer);
    refreshTimer = null;
  }
  if (refreshSeconds.value > 0) {
    refreshTimer = window.setInterval(() => {
      if (running.value || busy.value) return;
      if (document.visibilityState !== "visible") return;
      void load(true);
    }, refreshSeconds.value * 1000);
  }
}

watch(refreshSeconds, (value) => {
  localStorage.setItem(REFRESH_KEY, String(value));
  applyRefreshTimer();
});

onMounted(() => {
  void load();
  applyRefreshTimer();
});

onBeforeUnmount(() => {
  if (refreshTimer !== null) window.clearInterval(refreshTimer);
});

const failedResults = computed(() =>
  (runReport.value?.results ?? []).filter((result) => result.status === "failed"),
);
</script>

<template>
  <div>
    <PageHeader :title="t('activity.title')" :subtitle="t('activity.subtitle')">
      <template #actions>
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-control border border-line bg-surface px-3 py-2 text-body-sm text-ink-soft transition-colors hover:border-line-strong hover:text-ink disabled:opacity-60"
          :disabled="busy || running"
          @click="load(true)"
        >
          <RefreshCw :size="14" :stroke-width="1.9" :class="busy ? 'animate-spin' : ''" />
          {{ t("activity.redetect") }}
        </button>
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-control bg-brand px-3.5 py-2 text-body-sm font-medium text-white transition-colors hover:bg-brand-dark disabled:opacity-40"
          :disabled="!canRun"
          :title="actionable ? t('activity.runAllHint') : t('activity.runNoneHint')"
          @click="execute()"
        >
          <Loader2 v-if="running" :size="14" class="animate-spin" />
          <Wand2 v-else :size="14" :stroke-width="1.9" />
          {{
            running
              ? t("activity.running")
              : actionable
                ? t("activity.runCount", { count: actionable })
                : t("activity.runAll")
          }}
        </button>
      </template>
    </PageHeader>

    <div
      class="mt-4 flex flex-wrap items-center gap-x-8 gap-y-2 rounded-card border border-line bg-surface px-5 py-3 shadow-card"
    >
      <div class="flex items-baseline gap-1.5">
        <span class="text-metric font-semibold leading-none tabular-nums text-ink">
          {{ summary?.detectable ?? 0 }}/{{ summary?.accounts ?? 0 }}
        </span>
        <span class="text-caption text-muted">{{ t("activity.statAccounts") }}</span>
      </div>
      <div class="flex items-baseline gap-1.5">
        <span
          class="text-metric font-semibold leading-none tabular-nums"
          :class="actionable ? 'text-warn' : 'text-ink'"
        >
          {{ actionable }}
        </span>
        <span class="text-caption text-muted">{{ t("activity.statActionable") }}</span>
      </div>
      <div class="flex items-baseline gap-1.5">
        <span class="text-metric font-semibold leading-none tabular-nums text-ink">
          {{ summary?.done ?? 0 }}
        </span>
        <span class="text-caption text-muted">{{ t("activity.stateDone") }}</span>
      </div>
      <div class="flex items-baseline gap-1.5">
        <span class="text-metric font-semibold leading-none tabular-nums text-ink">
          {{ summary?.manual ?? 0 }}
        </span>
        <span class="text-caption text-muted">{{ t("activity.statManual") }}</span>
      </div>
      <span class="ml-auto text-caption text-muted">
        {{ t("activity.scopeSummary", { accounts: visibleAccounts.length, items: totalEntries }) }}
        <template v-if="detecting">{{ t("activity.detectingSuffix") }}</template>
      </span>
    </div>

    <div class="mt-4 flex flex-wrap items-center gap-3">
      <SegmentedControl
        :model-value="tab"
        :options="TAB_OPTIONS"
        :label="t('activity.tabsLabel')"
        @update:model-value="(value) => setTab(value as Tab)"
      />
      <span v-if="tab === 'official'" class="text-caption text-muted">
        {{ t("activity.officialTabHint") }}
      </span>
    </div>

    <div
      v-if="error"
      class="mt-3 rounded-card border border-line bg-danger-soft px-5 py-3 text-body-sm text-danger"
    >
      {{ t("activity.detectFailed", { message: error }) }}
    </div>

    <template v-if="tab === 'official'">
      <OfficialActivitiesPanel
        class="mt-3"
        :campaigns="campaigns"
        :accounts="pool"
        :catalog="catalogMeta"
        :detected-at="detectedAt"
        :runs="runs"
        :busy="busy"
        :running="running"
        :running-key="activeRunningKey"
        :initial-campaign="initialCampaign"
        @refresh="load(true)"
        @select="selectCampaign"
        @run="(accountIds, keys) => execute(accountIds, keys)"
      />
    </template>

    <template v-else-if="tab === 'runs'">
      <div class="mt-3">
        <ActivityRunsPanel :runs="runs" />
      </div>
    </template>

    <template v-else>
    <div class="mt-3 flex flex-wrap items-center gap-3">
      <div class="flex items-center gap-1 rounded-control border border-line bg-surface p-1">
        <button
          v-for="option in [
            { key: 'all', label: t('activity.filterAll') },
            { key: 'workbuddy', label: 'WorkBuddy' },
            { key: 'trae', label: 'Trae' },
          ]"
          :key="option.key"
          type="button"
          class="rounded-control px-2.5 py-1 text-body-sm transition-colors"
          :class="
            platformFilter === option.key
              ? 'bg-line-soft text-ink'
              : 'text-muted hover:text-ink-soft'
          "
          @click="platformFilter = option.key as 'all' | 'workbuddy' | 'trae'"
        >
          {{ option.label }}
        </button>
      </div>
      <label class="inline-flex cursor-pointer items-center gap-1.5 text-body-sm text-muted">
        <input v-model="hideDone" type="checkbox" class="size-3.5 accent-brand" />
        {{ t("activity.filterHideDone") }}
      </label>
      <SelectMenu
        v-model="refreshSeconds"
        :options="refreshOptions"
        :title="t('activity.refreshInterval')"
        size="sm"
        class="w-[128px]"
      />
      <span class="text-caption text-muted">{{ t("activity.cachedSuffix") }}</span>
    </div>

    <!-- 只有一张卡不等于加载失败：Trae 侧目前确实只有签到可自动化 -->
    <p v-if="platformFilter === 'trae'" class="mt-2 text-caption text-muted">
      {{ t("activity.traeScopeNote") }}
    </p>

    <div
      v-if="runReport"
      class="mt-3 rounded-card border border-line bg-surface px-5 py-2.5 text-caption"
    >
      <div class="flex flex-wrap items-center gap-2">
        <span class="text-muted">{{ t("activity.accountResultLabel") }}</span>
        <StatusPill v-if="runReport.summary.ok" tone="brand">
          {{ t("activity.resultOkCount", { count: runReport.summary.ok }) }}
        </StatusPill>
        <StatusPill v-if="runReport.summary.skipped" tone="neutral">
          {{ t("activity.resultSkippedCount", { count: runReport.summary.skipped }) }}
        </StatusPill>
        <StatusPill v-if="runReport.summary.failed" tone="danger">
          {{ t("activity.resultFailedCount", { count: runReport.summary.failed }) }}
        </StatusPill>
        <button
          type="button"
          class="ml-auto text-caption text-muted transition-colors hover:text-ink"
          @click="runReport = null"
        >
          {{ t("activity.hide") }}
        </button>
      </div>

      <!-- 失败明细：最多列 3 条；可重试的（网络 / 上游 5xx / Trae 9074）给一键重试。 -->
      <div v-if="failedResults.length" class="mt-1.5 space-y-1">
        <div
          v-for="item in failedResults.slice(0, 3)"
          :key="`${item.accountId}-${item.key}`"
          class="flex flex-wrap items-center gap-1.5 text-caption text-danger"
        >
          <span class="min-w-0 flex-1 truncate" :title="`${item.displayName}：${item.message}`">
            {{ item.displayName }}：{{ item.message }}
          </span>
          <button
            v-if="item.retryable"
            type="button"
            class="shrink-0 rounded-badge border border-line px-1.5 py-0.5 text-caption text-ink-soft transition-colors hover:border-brand hover:text-brand-dark disabled:cursor-not-allowed disabled:opacity-45"
            :disabled="running || busy"
            @click="runTargets([item.accountId])"
          >
            {{ t("activity.retryAccount") }}
          </button>
        </div>
        <div v-if="failedResults.length > 3" class="text-caption text-muted">
          {{ t("activity.moreFailures", { count: failedResults.length - 3 }) }}
        </div>
      </div>
    </div>

    <div v-if="!visibleAccounts.length" class="mt-4">
      <SectionCard>
        <div class="py-8 text-center text-body text-muted">
          {{ t("activity.noAccounts") }}
        </div>
      </SectionCard>
    </div>

    <div v-else-if="!activityGroups.length" class="mt-4">
      <SectionCard>
        <div class="flex items-center justify-center gap-2 py-8 text-body text-ink-soft">
          <CheckCircle2 :size="16" class="text-brand" />
          {{ t("activity.toastNothingToRun") }}
        </div>
      </SectionCard>
    </div>

    <SectionCard v-else class="mt-4" flush>
      <article
        v-for="group in activityGroups"
        :key="group.key"
        class="border-b border-line-soft last:border-b-0"
      >
        <div class="flex items-start gap-3 px-5 pt-3 pb-2">
          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span class="text-body font-medium text-ink">{{ group.title }}</span>
              <span
                class="rounded-full border border-line px-1.5 py-[1px] text-micro leading-tight text-muted"
              >
                {{ groupLabel(group.group) }}
              </span>
              <span class="text-caption text-muted">
                {{ PLATFORM_LABEL[group.platform] ?? group.platform }}
              </span>
              <StatusPill v-if="!group.automatable" tone="neutral">
                {{ t("activity.statManual") }}
              </StatusPill>
            </div>
            <p class="mt-0.5 truncate text-caption text-muted" :title="group.subtitle">
              {{ group.subtitle }}
            </p>
          </div>

          <div class="flex shrink-0 items-center gap-2">
            <div class="flex flex-col items-end gap-1">
              <div class="text-body-sm font-medium tabular-nums text-ink-soft">
                {{ completionText(group) }}
              </div>
              <div class="h-[3px] w-[88px] overflow-hidden rounded-full bg-line-soft">
                <div
                  class="h-full rounded-full transition-[width] duration-500"
                  :class="group.todo ? BAR_CLASS.todo : BAR_CLASS.done"
                  :style="{ width: `${donePercent(group)}%` }"
                />
              </div>
              <div v-if="group.todo" class="text-caption font-medium text-warn">
                {{ t("activity.todoCount", { count: group.todo }) }}
              </div>
              <div v-else-if="group.unavailable" class="text-caption text-danger">
                {{ t("activity.notQueryable") }} {{ group.unavailable }}
              </div>
            </div>
            <button
              v-if="group.todo"
              type="button"
              class="inline-flex items-center gap-1 rounded-control border border-line px-2.5 py-1.5 text-caption text-ink-soft transition-colors hover:border-line-strong hover:text-ink disabled:opacity-50"
              :disabled="running"
              :title="t('activity.runThisAccountHint')"
              @click="
                execute(
                  group.entries.filter((entry) => entry.actionable).map((entry) => entry.accountId),
                )
              "
            >
              <Wand2 :size="12" :stroke-width="1.9" />
              {{ t("activity.run", { count: group.todo }) }}
            </button>
            <button
              type="button"
              class="inline-flex items-center gap-0.5 rounded-control px-1.5 py-1.5 text-caption text-muted transition-colors hover:text-ink"
              :title="expanded.has(group.key) ? t('activity.hide') : t('activity.expandDetail')"
              @click="toggleDetail(group.key)"
            >
              {{ expanded.has(group.key) ? t("activity.hide") : t("activity.expandDetail") }}
              <ChevronDown
                :size="12"
                class="transition-transform"
                :class="expanded.has(group.key) ? 'rotate-180' : ''"
              />
            </button>
          </div>
        </div>

        <div class="flex flex-wrap gap-1.5 px-5 pt-1 pb-3">
          <span
            v-for="entry in group.entries"
            :key="`${group.key}-${entry.accountId}`"
            class="inline-flex max-w-[180px] items-center gap-1 rounded-control border px-2 py-[3px] text-caption leading-tight"
            :class="CHIP_CLASS[entry.state]"
            :title="`${entry.name} · ${t(STATE_TEXT[entry.state])}${
              entry.progressText ? ` · ${entry.progressText}` : ''
            }${entry.manualReason ? ` · ${entry.manualReason}` : ''}`"
          >
            <component :is="STATE_ICON[entry.state]" :size="10" :stroke-width="2.2" class="shrink-0" />
            <span class="truncate">{{ entry.name }}</span>
            <Loader2
              v-if="running && runningTargets.has(entry.accountId) && entry.actionable"
              :size="9"
              class="shrink-0 animate-spin"
            />
            <span v-else-if="entry.state === 'done'" class="shrink-0 opacity-70">100%</span>
            <span v-else-if="entry.percent > 0" class="shrink-0 opacity-70 tabular-nums">
              {{ entry.percent }}%
            </span>
          </span>
        </div>

        <div v-if="expanded.has(group.key)" class="border-t border-line-soft px-5 py-3">
          <p class="text-caption leading-relaxed text-muted">{{ group.description }}</p>
          <div class="mt-2 grid gap-1.5 sm:grid-cols-2">
            <div
              v-for="entry in group.entries.filter((row) => row.state !== 'done' && row.state !== 'undetected')"
              :key="`detail-${group.key}-${entry.accountId}`"
              class="rounded-control border border-line-soft bg-canvas/60 px-2.5 py-2 text-caption"
            >
              <div class="flex items-center gap-1.5">
                <component :is="STATE_ICON[entry.state]" :size="11" class="shrink-0" />
                <span class="truncate font-medium text-ink-soft">{{ entry.name }}</span>
                <span class="ml-auto shrink-0 text-muted">{{ t(STATE_TEXT[entry.state]) }}</span>
              </div>
              <div v-if="entry.progressText" class="mt-0.5 text-muted">{{ entry.progressText }}</div>
              <div v-if="entry.note" class="mt-0.5 leading-relaxed text-muted">{{ entry.note }}</div>
              <div v-if="entry.state === 'unavailable'" class="mt-0.5 text-danger">
                {{ t("activity.notFoundWarning") }}
              </div>
              <div v-if="entry.rewardText" class="mt-0.5 text-ink-soft">{{ entry.rewardText }}</div>
              <div v-else-if="entry.rewardHint" class="mt-0.5 text-muted">{{ entry.rewardHint }}</div>
              <div v-if="entry.period" class="mt-0.5 text-muted">{{ entry.period }}</div>
              <div
                v-if="!group.automatable && entry.manualTasks.length"
                class="mt-1 space-y-0.5 border-t border-line-soft pt-1"
              >
                <div
                  v-for="task in entry.manualTasks"
                  :key="task.code"
                  class="flex items-start gap-1 text-muted"
                >
                  <span class="mt-[4px] size-1 shrink-0 rounded-full bg-warn" />
                  <a
                    v-if="task.jumpUrl"
                    :href="task.jumpUrl"
                    target="_blank"
                    rel="noreferrer"
                    class="text-ink-soft hover:text-brand hover:underline"
                    :title="task.desc || task.title"
                  >
                    {{ task.title }}
                    <ExternalLink :size="8" :stroke-width="2" class="inline align-baseline" />
                  </a>
                  <span v-else :title="task.desc">{{ task.title }}</span>
                </div>
              </div>
              <div v-else-if="!group.automatable" class="mt-0.5 text-muted">
                {{ entry.manualReason || t("activity.manualFallback") }}
              </div>
              <a
                v-if="entry.jumpUrl && !entry.manualTasks.length"
                :href="entry.jumpUrl"
                target="_blank"
                rel="noreferrer"
                class="mt-1 inline-flex items-center gap-0.5 text-brand hover:underline"
              >
                {{ t("activity.openInClient") }}
                <ExternalLink :size="9" :stroke-width="2" />
              </a>
            </div>
          </div>
        </div>
      </article>
    </SectionCard>
    </template>

    <Teleport to="body">
      <div
        v-if="toast"
        class="fixed bottom-8 left-1/2 z-[60] -translate-x-1/2 rounded-control bg-ink px-4 py-2.5 text-body-sm text-white"
      >
        {{ toast }}
      </div>
    </Teleport>

    <ConfirmDialog
      :open="pendingRun !== null"
      :title="t('activity.noticeTitle')"
      :message="t('activity.noticeBody')"
      :confirm-text="t('activity.noticeConfirm')"
      @confirm="confirmNotice"
      @cancel="cancelNotice"
    />
  </div>
</template>

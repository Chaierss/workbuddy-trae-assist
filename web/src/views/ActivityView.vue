<script setup lang="ts">
/**
 * 活动页。
 *
 * 渲染顺序是刻意的：**先展示有哪些活动，再去检测**。
 *   1. 目录（`/activities/catalog`，零网络）+ 本地号池清单 → 立刻铺出骨架卡片（「未检测」）；
 *   2. 再拉 `/activities` 逐个账号检测 → 按 key 覆盖成真实状态/进度/收益。
 * 这样冷启动不会先看到一片空白，用户也不会以为「没活动」。
 *
 * 三条纪律写死在界面上：
 *   1. 查不了就写「不可查」+ 原因，绝不显示成「没有活动」；
 *   2. 不可自动化的项必须写明理由，而不是悄悄不出现；
 *   3. 一键执行后如实汇报「成功 / 跳过 / 失败」，跳过也要给出原因。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch, type Component } from "vue";
import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  Clock,
  ExternalLink,
  Loader2,
  RefreshCw,
  Wand2,
} from "lucide-vue-next";
import SectionCard from "../components/SectionCard.vue";
import SelectMenu from "../components/SelectMenu.vue";
import StatCard from "../components/StatCard.vue";
import StatusPill from "../components/StatusPill.vue";
import { t, type MessageKey } from "../lib/i18n";
import {
  api,
  formatTime,
  type AccountActivities,
  type ActivityCatalogEntry,
  type ActivityItem,
  type ActivityRunReport,
  type ActivityState,
  type ActivitySummary,
} from "../api";

/** 卡片状态比检测状态多一档「未检测」——目录已铺出、检测还没回来。 */
type CardState = ActivityState | "undetected";

/** 卡片状态文案存 key；模块级常量里不能直接 `t()`，否则切语言不会更新。 */
const STATE_TEXT: Record<CardState, MessageKey> = {
  done: "activity.stateDone",
  todo: "activity.stateTodo",
  in_progress: "activity.stateInProgress",
  not_started: "activity.stateNotStarted",
  unavailable: "activity.stateUnavailable",
  undetected: "activity.stateUndetected",
};

const STATE_TONE: Record<CardState, "neutral" | "brand" | "warn" | "danger" | "info"> = {
  done: "brand",
  todo: "warn",
  in_progress: "info",
  not_started: "neutral",
  unavailable: "danger",
  undetected: "neutral",
};

const STATE_ICON: Record<CardState, Component> = {
  done: CheckCircle2,
  todo: CircleDashed,
  in_progress: Clock,
  not_started: CircleDashed,
  unavailable: AlertTriangle,
  undetected: CircleDashed,
};

const STATE_ICON_CLASS: Record<CardState, string> = {
  done: "text-brand",
  todo: "text-warn",
  in_progress: "text-info",
  not_started: "text-muted",
  unavailable: "text-danger",
  undetected: "text-muted",
};

/** 进度条配色：与状态药丸同一套语义。 */
const BAR_CLASS: Record<CardState, string> = {
  done: "bg-brand",
  todo: "bg-warn",
  in_progress: "bg-info",
  not_started: "bg-line",
  unavailable: "bg-danger",
  undetected: "bg-line",
};

/** 长短期分类：短期每天重置，长期在活动期内累计。 */
const GROUP_LABEL: Record<string, MessageKey> = {
  daily: "activity.groupDaily",
  seasonal: "activity.groupSeasonal",
};

const PLATFORM_LABEL: Record<string, string> = {
  workbuddy: "WorkBuddy",
  trae: "Trae",
};

/** 取长短期分类文案：未知分组回落成原始值。 */
function groupLabel(group: string): string {
  return GROUP_LABEL[group] ? t(GROUP_LABEL[group]) : group;
}

/** 一张活动卡片：目录项与检测结果按 key 合二为一。 */
interface ActivityCard {
  key: string;
  group: string;
  title: string;
  subtitle: string;
  description: string;
  rewardHint: string;
  state: CardState;
  automatable: boolean;
  manualReason: string;
  progressCurrent: number | null;
  progressTarget: number | null;
  progressText: string;
  rewardText: string;
  periodStart: string | null;
  periodEnd: string | null;
  note: string;
  /** 手动项的上游深链（有则给一个「去客户端完成」入口）。 */
  jumpUrl: string;
  /** 逐条的手动任务：标题 + 说明 + 深链。 */
  manualTasks: Array<{ code: string; title: string; desc: string; jumpUrl: string }>;
  /** false = 这一项还没检测（目录骨架）。 */
  detected: boolean;
}

/** 一个账号 + 它的卡片集合。 */
interface AccountView {
  accountId: string;
  displayName: string;
  platform: string;
  scope: string;
  detectable: boolean;
  detected: boolean;
  error: string | null;
  updatedAt: string | null;
  cards: ActivityCard[];
  actionable: number;
}

const catalog = ref<ActivityCatalogEntry[]>([]);
/** 本地号池清单：目录铺骨架用，不访问上游。 */
const knownAccounts = ref<AccountBase[]>([]);
const pool = ref<AccountActivities[]>([]);
const summary = ref<ActivitySummary | null>(null);
const busy = ref(false);
const running = ref(false);
const error = ref<string | null>(null);
const platformFilter = ref<"all" | "workbuddy" | "trae">("all");
const hideDone = ref(false);
const runReport = ref<ActivityRunReport | null>(null);
/** 正在执行的账号：进度直接画在该账号卡片内部，不再单开「上次执行」面板。 */
const runningAccounts = ref<Set<string>>(new Set());
const toast = ref<string | null>(null);

function fromCatalog(entry: ActivityCatalogEntry, item?: ActivityItem): ActivityCard {
  if (!item) {
    return {
      key: entry.key,
      group: entry.group,
      title: entry.title,
      subtitle: entry.subtitle,
      description: entry.description,
      rewardHint: entry.rewardHint,
      state: "undetected",
      automatable: entry.automatable,
      manualReason: "",
      progressCurrent: null,
      progressTarget: null,
      progressText: "",
      rewardText: "",
      periodStart: null,
      periodEnd: null,
      note: "",
      jumpUrl: "",
      manualTasks: [],
      detected: false,
    };
  }
  return fromItem(item, entry.description, entry.rewardHint);
}

function fromItem(item: ActivityItem, description = "", rewardHint = ""): ActivityCard {
  return {
    key: item.key,
    group: item.group,
    title: item.title,
    subtitle: item.subtitle,
    description,
    rewardHint,
    state: item.state,
    automatable: item.automatable,
    manualReason: item.manualReason,
    progressCurrent: item.progressCurrent,
    progressTarget: item.progressTarget,
    progressText: item.progressText,
    rewardText: item.rewardText,
    periodStart: item.periodStart,
    periodEnd: item.periodEnd,
    note: item.note,
    jumpUrl: item.jumpUrl ?? "",
    manualTasks: item.manualTasks ?? [],
    detected: true,
  };
}

/** 目录项（按平台）在前，检测出来的额外项（目录里没有）也要显示，绝不吞掉。 */
function cardsFor(platform: string, items: ActivityItem[]): ActivityCard[] {
  const detected = new Map(items.map((item) => [item.key, item]));
  const seen = new Set<string>();
  const cards: ActivityCard[] = [];
  for (const entry of catalog.value) {
    if (entry.platform !== platform) continue;
    seen.add(entry.key);
    cards.push(fromCatalog(entry, detected.get(entry.key)));
  }
  for (const item of items) {
    if (seen.has(item.key)) continue;
    cards.push(fromItem(item));
  }
  return hideDone.value ? cards.filter((card) => card.state !== "done") : cards;
}

/** 本地号池里的账号（骨架期只有这些字段）。 */
interface AccountBase {
  id: string;
  displayName: string;
  platform: string;
  scope: string;
}

function toView(account: AccountActivities | null, base: AccountBase): AccountView {
  const cards = cardsFor(base.platform, account?.activities ?? []);
  return {
    accountId: base.id,
    displayName: base.displayName,
    platform: base.platform,
    scope: base.scope,
    detectable: account ? account.detectable : true,
    detected: Boolean(account),
    error: account?.error ?? null,
    updatedAt: account?.updatedAt ?? null,
    cards,
    actionable: cards.filter((card) => isCardActionable(card)).length,
  };
}

const viewAccounts = computed<AccountView[]>(() => {
  const detected = new Map(pool.value.map((item) => [item.accountId, item]));
  const list: AccountView[] = [];
  const seen = new Set<string>();
  for (const base of knownAccounts.value) {
    seen.add(base.id);
    list.push(toView(detected.get(base.id) ?? null, base));
  }
  // 检测结果里有、但本地清单没列出的账号（极少见）也补上。
  for (const item of pool.value) {
    if (seen.has(item.accountId)) continue;
    list.push(
      toView(item, {
        id: item.accountId,
        displayName: item.displayName,
        platform: item.platform,
        scope: item.scope,
      }),
    );
  }
  return list.filter(
    (account) => platformFilter.value === "all" || account.platform === platformFilter.value,
  );
});

const actionable = computed(() => summary.value?.actionable ?? 0);
const canRun = computed(() => actionable.value > 0 && !running.value && !busy.value);
/** 检测还没回来：显示「正在逐个账号检测」。 */
const detecting = computed(() => busy.value && !pool.value.length);

function isCardActionable(card: ActivityCard): boolean {
  return card.detected && card.automatable && card.state === "todo";
}

/** 完成度百分比。上游没给比例时不编数字，用固定档表达「进行中」。 */
function completionPercent(card: ActivityCard): number {
  if (card.state === "done") return 100;
  if (card.state !== "todo" && card.state !== "in_progress") return 0;
  const { progressCurrent: current, progressTarget: target } = card;
  if (current !== null && target !== null && target > 0) {
    return Math.max(0, Math.min(100, Math.round((current / target) * 100)));
  }
  return card.state === "in_progress" ? 50 : 0;
}

function completionText(card: ActivityCard): string {
  if (card.progressText) return card.progressText;
  if (card.state === "undetected") return t("activity.stateUndetected");
  if (card.state === "unavailable") return t("activity.completionNotQueryable");
  if (card.state === "done") return "100%";
  if (card.state === "in_progress") return t("activity.stateInProgress");
  if (card.state === "todo") return t("activity.stateTodo");
  return "—";
}

/** 只有「进行中但上游没给比例」才需要解释这个 50% 从哪来。 */
function progressNote(card: ActivityCard): string {
  const hasRatio = card.progressCurrent !== null && card.progressTarget !== null;
  return card.state === "in_progress" && !hasRatio ? t("activity.upstreamNoRatio") : "";
}

function periodText(card: ActivityCard): string {
  const start = card.periodStart?.slice(0, 10);
  const end = card.periodEnd?.slice(0, 10);
  if (!start && !end) return "";
  return `${start ?? "?"} ~ ${end ?? "?"}`;
}

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
    // 目录与号池清单都是本地数据，先回来就先铺骨架。
    const [catalogResult, accountsResult] = await Promise.all([
      api.activityCatalog(),
      api.mergedAccounts(),
    ]);
    catalog.value = catalogResult.entries;
    knownAccounts.value = accountsResult.accounts.map((account) => ({
      id: account.id,
      displayName: account.displayName,
      platform: account.platform,
      scope: account.scope,
    }));

    // 再等真实检测结果按 key 覆盖。
    const data = await api.activities(refresh);
    pool.value = data.accounts;
    summary.value = data.summary;
  } catch (cause) {
    error.value = (cause as Error).message;
  } finally {
    busy.value = false;
  }
}

async function execute(accountIds?: string[]): Promise<void> {
  if (running.value) return;
  const targets = accountIds?.length
    ? accountIds
    : viewAccounts.value
        .filter((account) => account.actionable > 0)
        .map((account) => account.accountId);
  running.value = true;
  runningAccounts.value = new Set(targets);
  try {
    const report = await api.runActivities(accountIds);
    runReport.value = report;
    const { ok, skipped, failed } = report.summary;
    if (!report.summary.total) {
      showToast(t("activity.toastNothingToRun"));
    } else {
      showToast(t("activity.toastRunDone", { ok, skipped, failed }));
    }
    // 执行后状态已变，直接绕过缓存重读。
    await load(true);
  } catch (cause) {
    showToast((cause as Error).message);
  } finally {
    running.value = false;
    runningAccounts.value = new Set();
  }
}

// ---------- 自动刷新（与号池同一套频率语义） ----------

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
      // 执行中不打断（后端有执行锁），切到后台也不打请求。
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

/** 单个账号这次的执行结果（成功了什么、跳过了什么、失败了什么）。 */
const reportByAccount = computed(() => {
  const map = new Map<string, ActivityRunReport["results"]>();
  for (const result of runReport.value?.results ?? []) {
    const list = map.get(result.accountId) ?? [];
    list.push(result);
    map.set(result.accountId, list);
  }
  return map;
});

function accountResults(accountId: string): ActivityRunReport["results"] {
  return reportByAccount.value.get(accountId) ?? [];
}

function countResults(accountId: string, status: "ok" | "skipped" | "failed"): number {
  return accountResults(accountId).filter((result) => result.status === status).length;
}

function resultMessage(accountId: string, status: "ok" | "skipped" | "failed"): string {
  const hit = accountResults(accountId).find((result) => result.status === status);
  return hit?.message ?? "";
}
</script>

<template>
  <div>
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <h1 class="text-[23.5px] font-semibold tracking-[-0.02em] text-ink">{{ t("activity.title") }}</h1>
        <p class="mt-1 max-w-[640px] text-[12.5px] leading-relaxed text-muted">
          {{ t("activity.subtitle") }}
        </p>
      </div>
      <div class="flex shrink-0 items-center gap-2">
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-[9px] border border-line bg-surface px-3 py-2 text-[12.5px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink disabled:opacity-60"
          :disabled="busy || running"
          @click="load(true)"
        >
          <RefreshCw :size="14" :stroke-width="1.9" :class="busy ? 'animate-spin' : ''" />
          {{ t("activity.redetect") }}
        </button>
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-[9px] bg-ink px-3.5 py-2 text-[12.5px] text-white transition-opacity hover:opacity-90 disabled:opacity-40"
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
      </div>
    </div>

    <div class="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <StatCard
        :label="t('activity.statAccounts')"
        :value="`${summary?.detectable ?? 0}/${summary?.accounts ?? 0}`"
        :icon="CheckCircle2"
        :hint="t('activity.statAccountsHint')"
      />
      <StatCard
        :label="t('activity.statActionable')"
        :value="String(actionable)"
        :icon="Wand2"
        :hint="
          actionable ? t('activity.statActionableHintOn') : t('activity.statActionableHintDone')
        "
      />
      <StatCard
        :label="t('activity.stateDone')"
        :value="String(summary?.done ?? 0)"
        :icon="CheckCircle2"
        :hint="t('activity.statDoneHint')"
      />
      <StatCard
        :label="t('activity.statManual')"
        :value="String(summary?.manual ?? 0)"
        :icon="AlertTriangle"
        :hint="t('activity.statManualHint')"
      />
    </div>

    <div
      v-if="error"
      class="mt-4 rounded-[var(--radius-card)] border border-line bg-danger-soft px-5 py-3 text-[12.5px] text-danger"
    >
      {{ t("activity.detectFailed", { message: error }) }}
    </div>

    <div class="mt-4 flex flex-wrap items-center gap-3">
      <div class="flex items-center gap-1 rounded-[9px] border border-line bg-surface p-1">
        <button
          v-for="option in [
            { key: 'all', label: t('activity.filterAll') },
            { key: 'workbuddy', label: 'WorkBuddy' },
            { key: 'trae', label: 'Trae' },
          ]"
          :key="option.key"
          type="button"
          class="rounded-[7px] px-2.5 py-1 text-[12px] transition-colors"
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
      <label class="inline-flex cursor-pointer items-center gap-1.5 text-[12px] text-muted">
        <input v-model="hideDone" type="checkbox" class="size-3.5 accent-[#16a34a]" />
        {{ t("activity.filterHideDone") }}
      </label>
      <SelectMenu
        v-model="refreshSeconds"
        :options="refreshOptions"
        :title="t('activity.refreshInterval')"
        size="sm"
        class="w-[128px]"
      />
      <span class="text-[12px] text-muted">
        {{ t("activity.scopeSummary", { accounts: viewAccounts.length, items: catalog.length }) }}
        <template v-if="detecting">{{ t("activity.detectingSuffix") }}</template>
        <template v-else>{{ t("activity.cachedSuffix") }}</template>
      </span>
    </div>

    <div v-if="!viewAccounts.length" class="mt-4">
      <SectionCard>
        <div class="py-8 text-center text-[13px] text-muted">
          {{ t("activity.noAccounts") }}
        </div>
      </SectionCard>
    </div>

    <div v-else class="mt-4 space-y-4">
      <SectionCard
        v-for="account in viewAccounts"
        :key="account.accountId"
        :title="account.displayName"
        :subtitle="`${PLATFORM_LABEL[account.platform] ?? account.platform}${
          account.detected && account.updatedAt
            ? t('activity.detectedAt', { time: formatTime(account.updatedAt) })
            : ''
        }`"
        flush
      >
        <template #actions>
          <button
            v-if="account.actionable > 0"
            type="button"
            class="inline-flex items-center gap-1.5 rounded-[8px] border border-line px-2.5 py-1.5 text-[12px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink disabled:opacity-50"
            :disabled="running"
            @click="execute([account.accountId])"
          >
            <Wand2 :size="12" :stroke-width="1.9" />
            {{ t("activity.runAccountCount", { count: account.actionable }) }}
          </button>
          <StatusPill v-else-if="account.detected && !account.detectable" tone="danger">
            {{ t("activity.notQueryable") }}
          </StatusPill>
        </template>

        <div
          v-if="runningAccounts.has(account.accountId)"
          class="border-b border-line-soft px-5 py-2.5"
        >
          <div class="flex items-center justify-between gap-3 text-[11.5px] text-ink-soft">
            <span class="inline-flex items-center gap-1.5">
              <Loader2 :size="12" :stroke-width="2.2" class="animate-spin text-brand" />
              {{ t("activity.accountRunning", { count: account.actionable }) }}
            </span>
            <span class="truncate text-muted">{{ t("activity.accountRunningHint") }}</span>
          </div>
          <div class="mt-1.5 h-1.5 overflow-hidden rounded-full bg-line-soft">
            <div class="run-fill h-full rounded-full bg-brand"></div>
          </div>
        </div>

        <div
          v-else-if="accountResults(account.accountId).length"
          class="flex flex-wrap items-center gap-2 border-b border-line-soft px-5 py-2"
        >
          <span class="text-[11.5px] text-muted">{{ t("activity.accountResultLabel") }}</span>
          <StatusPill v-if="countResults(account.accountId, 'ok')" tone="brand">
            {{ t("activity.resultOkCount", { count: countResults(account.accountId, "ok") }) }}
          </StatusPill>
          <StatusPill v-if="countResults(account.accountId, 'skipped')" tone="neutral">
            {{
              t("activity.resultSkippedCount", {
                count: countResults(account.accountId, "skipped"),
              })
            }}
          </StatusPill>
          <StatusPill v-if="countResults(account.accountId, 'failed')" tone="danger">
            {{ t("activity.resultFailedCount", { count: countResults(account.accountId, "failed") }) }}
          </StatusPill>
          <span
            v-if="resultMessage(account.accountId, 'failed')"
            class="min-w-0 flex-1 truncate text-[11px] text-danger"
            :title="resultMessage(account.accountId, 'failed')"
          >
            {{ resultMessage(account.accountId, "failed") }}
          </span>
          <button
            type="button"
            class="ml-auto text-[11px] text-muted transition-colors hover:text-ink"
            @click="runReport = null"
          >
            {{ t("activity.hide") }}
          </button>
        </div>

        <div
          v-if="account.detected && !account.detectable"
          class="flex items-start gap-2.5 px-5 py-4 text-[12.5px] text-ink-soft"
        >
          <AlertTriangle :size="15" class="mt-0.5 shrink-0 text-warn" />
          <div>
            <div class="font-medium text-ink">{{ t("activity.accountUnqueryable") }}</div>
            <div class="mt-0.5 leading-relaxed text-muted">
              {{ account.error ?? t("activity.noCredentialSupport") }}
            </div>
          </div>
        </div>

        <div v-else class="px-5 py-4">
          <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            <article
              v-for="card in account.cards"
              :key="card.key"
              class="flex flex-col gap-2.5 rounded-[var(--radius-card)] border border-line bg-surface p-4 shadow-card transition-colors hover:border-[#d7dad5]"
            >
              <header class="flex items-start gap-2">
                <component
                  :is="STATE_ICON[card.state]"
                  :size="15"
                  :stroke-width="1.9"
                  class="mt-0.5 shrink-0"
                  :class="STATE_ICON_CLASS[card.state]"
                />
                <div class="min-w-0 flex-1">
                  <div class="flex flex-wrap items-center gap-1.5">
                    <span class="truncate text-[13px] font-medium text-ink">{{ card.title }}</span>
                    <StatusPill :tone="STATE_TONE[card.state]">
                      {{ t(STATE_TEXT[card.state]) }}
                    </StatusPill>
                  </div>
                  <div class="mt-0.5 truncate text-[11.5px] text-muted">{{ card.subtitle }}</div>
                </div>
                <span
                  class="shrink-0 rounded-full border border-line px-1.5 py-[1px] text-[10.5px] leading-tight text-muted"
                >
                  {{ groupLabel(card.group) }}
                </span>
              </header>

              <p class="text-[11.5px] leading-relaxed text-muted">{{ card.description }}</p>

              <div>
                <div class="flex items-center justify-between text-[11px] text-muted">
                  <span>{{ t("activity.completion") }}</span>
                  <span class="text-ink-soft">{{ completionText(card) }}</span>
                </div>
                <div class="mt-1 h-1.5 overflow-hidden rounded-full bg-line-soft">
                  <div
                    class="h-full rounded-full transition-[width] duration-500"
                    :class="BAR_CLASS[card.state]"
                    :style="{ width: `${completionPercent(card)}%` }"
                  />
                </div>
              </div>

              <div v-if="card.note" class="text-[11.5px] leading-relaxed text-muted">
                {{ card.note }}
              </div>
              <div v-if="progressNote(card)" class="text-[11px] text-muted">{{ progressNote(card) }}</div>
              <div v-if="card.state === 'unavailable'" class="text-[11px] text-danger">
                {{ t("activity.notFoundWarning") }}
              </div>

              <div
                v-if="!card.automatable && card.state !== 'done' && card.state !== 'undetected'"
                class="rounded-[8px] border border-line-soft bg-line-soft px-2.5 py-2 text-[11px] leading-relaxed text-muted"
              >
                <div>
                  <span class="font-medium text-ink-soft">{{ t("activity.statManual") }}</span>
                  · {{ card.manualReason || t("activity.manualFallback") }}
                </div>
                <!-- 逐条列出仍需手动完成的项：点标题直接唤起客户端对应页面 -->
                <ul v-if="card.manualTasks.length" class="mt-1.5 space-y-1">
                  <li v-for="task in card.manualTasks" :key="task.code" class="flex gap-1">
                    <span class="mt-[3px] size-1 shrink-0 rounded-full bg-warn" />
                    <div class="min-w-0">
                      <a
                        v-if="task.jumpUrl"
                        :href="task.jumpUrl"
                        target="_blank"
                        rel="noreferrer"
                        class="font-medium text-ink-soft hover:text-brand hover:underline"
                        :title="task.desc || task.title"
                      >
                        {{ task.title }}
                        <ExternalLink :size="9" :stroke-width="2" class="inline align-baseline" />
                      </a>
                      <span v-else class="font-medium text-ink-soft" :title="task.desc">
                        {{ task.title }}
                      </span>
                    </div>
                  </li>
                </ul>
                <a
                  v-else-if="card.jumpUrl"
                  :href="card.jumpUrl"
                  target="_blank"
                  rel="noreferrer"
                  class="mt-1 inline-flex items-center gap-0.5 text-brand hover:underline"
                >
                  {{ t("activity.openInClient") }}
                  <ExternalLink :size="10" :stroke-width="2" />
                </a>
              </div>

              <div class="mt-auto flex items-end justify-between gap-2">
                <div class="min-w-0">
                  <div v-if="card.rewardText" class="text-[12.5px] font-medium text-ink">
                    {{ card.rewardText }}
                  </div>
                  <div
                    v-else-if="!card.detected || card.state === 'not_started'"
                    class="text-[11.5px] leading-snug text-muted"
                  >
                    {{ card.rewardHint }}
                  </div>
                  <div v-if="periodText(card)" class="mt-0.5 text-[11px] text-muted">
                    {{ periodText(card) }}
                  </div>
                </div>
                <button
                  v-if="isCardActionable(card)"
                  type="button"
                  class="inline-flex shrink-0 items-center gap-1 rounded-[8px] border border-line px-2.5 py-1.5 text-[11.5px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink disabled:opacity-50"
                  :disabled="running"
                  :title="t('activity.runThisAccountHint')"
                  @click="execute([account.accountId])"
                >
                  <Wand2 :size="12" :stroke-width="1.9" />
                  {{ t("activity.run") }}
                </button>
              </div>
            </article>
          </div>
          <div v-if="!account.cards.length" class="py-6 text-center text-[12.5px] text-muted">
            {{ t("activity.noCards") }}
          </div>
        </div>
      </SectionCard>
    </div>

    <Teleport to="body">
      <div
        v-if="toast"
        class="fixed bottom-8 left-1/2 z-[60] -translate-x-1/2 rounded-[9px] bg-ink px-4 py-2.5 text-[12.5px] text-white"
      >
        {{ toast }}
      </div>
    </Teleport>
  </div>
</template>

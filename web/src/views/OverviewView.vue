<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { RouterLink, useRouter } from "vue-router";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Coins,
  Database,
  HeartPulse,
  Loader2,
  MonitorSmartphone,
  Network,
  ShieldCheck,
  Users,
} from "lucide-vue-next";
import PageHeader from "../components/PageHeader.vue";
import SectionCard from "../components/SectionCard.vue";
import StatCard from "../components/StatCard.vue";
import StatusPill from "../components/StatusPill.vue";
import Skeleton from "../components/Skeleton.vue";
import OnboardingChecklist from "../components/onboarding/OnboardingChecklist.vue";
import OfficialActivityStrip from "../components/OfficialActivityStrip.vue";
import { api, formatAmount, formatTime, type ActionableItem, type ActivityCampaign, type ActivityRewards, type OverviewPulse, type Snapshot, type StabilityReport } from "../api";
import { t } from "../lib/i18n";

import {
  accounts,
  attentionCount,
  clientDetections,
  credentialIssueCounts,
  detection,
  healthAutoStats,
  healthUnhandled,
  loadHealthBadge,
  scopedPath,
} from "../stores/app";
const router = useRouter();

/**
 * 跳转地址统一补作用域前缀（`/s/<scope>/…`），和侧栏链接保持一致：
 * 这样从概览点进去后，侧栏 `RouterLink` 的 `isActive` 才能正确高亮。
 * 带 query 的地址拆开处理，query 原样带回。
 */
function navTo(to: string): string {
  const queryIndex = to.indexOf("?");
  const path = queryIndex >= 0 ? to.slice(0, queryIndex) : to;
  const query = queryIndex >= 0 ? to.slice(queryIndex) : "";
  return scopedPath(path) + query;
}

const currentAccount = computed(() => {
  const externalId = detection.value?.currentAccountId;
  if (!externalId) return null;
  return accounts.value.find((item) => item.externalId === externalId) ?? null;
});

const subtitle = computed(() => {
  const suffix = detection.value?.loggedIn
    ? currentAccount.value
      ? t("overview.subtitle.login", { name: currentAccount.value.displayName })
      : t("overview.subtitle.notImported")
    : t("overview.clientLoggedOut");
  return t("overview.subtitle", { status: suffix });
});

const actionables = ref<ActionableItem[]>([]);
const pulse = ref<OverviewPulse | null>(null);

/** 官方活动速览：与待处理清单同一次请求返回，不额外打上游。 */
const campaigns = ref<ActivityCampaign[]>([]);
/** 活动收益：只含本工具代做记录里的奖励，读不到就不显示这块，不编数字。 */
const rewards = ref<ActivityRewards | null>(null);
const rewardsLoading = ref(true);
const rewardsError = ref("");
const loadError = ref("");
const actionablesLoading = ref(true);

/** 近 7 天稳定度：结论条只说「有几个号失败过」，明细去账号页看。 */
const stability = ref<StabilityReport | null>(null);

async function loadStability(): Promise<void> {
  try {
    stability.value = await api.poolStability(7);
  } catch {
    // 读不到就不显示这一句；不能用 0 冒充「一切正常」。
    stability.value = null;
  }
}

/** 7 天窗口里出现过任一失败的号数（切换 / 登录失效 / 续期 / 网关）。 */
const unstableCount = computed(() => {
  const items = stability.value?.items ?? [];
  return items.filter(
    (item) =>
      (item.loginInvalid ?? 0) > 0 ||
      (item.refreshFailed ?? 0) > 0 ||
      (item.gatewayFailed ?? 0) > 0 ||
      (item.switchTotal > 0 && item.switchOk < item.switchTotal),
  ).length;
});

/**
 * 结论条：一眼回答「今天要不要管」。
 * 四态：空池 / 全好 / 全好但有健康事件 / 有凭据问题；文案在 locales，不在这里拼。
 */
const conclusion = computed(() => {
  const total = accounts.value.length;
  if (!total) {
    return {
      tone: "neutral" as const,
      icon: Users,
      text: t("overview.conclusion.empty"),
      cta: t("overview.conclusion.emptyCta"),
      to: navTo("/accounts"),
    };
  }
  const issues = attentionCount.value;
  if (issues === 0) {
    if (healthUnhandled.value > 0) {
      return {
        tone: "neutral" as const,
        icon: ShieldCheck,
        text: t("overview.conclusion.healthyPending", { total, pending: healthUnhandled.value }),
        cta: t("overview.conclusion.openHealth"),
        to: navTo("/health"),
      };
    }
    return {
      tone: "ok" as const,
      icon: ShieldCheck,
      text: t("overview.conclusion.healthy", { total }),
      cta: t("overview.conclusion.openAccounts"),
      to: navTo("/accounts"),
    };
  }
  return {
    tone: "warn" as const,
    icon: AlertTriangle,
    text: t("overview.conclusion.issues", { total, healthy: total - issues, issues }),
    cta: t("overview.conclusion.handle"),
    to: navTo("/accounts?risk=credential"),
  };
});
const busy = ref<string | null>(null);
const runError = ref("");
/** 客户端登录态目录默认收成一行，点开才铺完整路径 —— 它不是常用信息。 */
const expandedClients = ref<Set<string>>(new Set());

/** 页面本地的引导态已由首启清单（`OnboardingChecklist`）承载，不再进待处理组。 */
const items = computed<ActionableItem[]>(() => actionables.value);

/**
 * 待处理按 `kind` 聚合成组。
 *
 * 后端是逐账号出条目的（一个 kind 一账号一条）：16 个账号没签到就是 16 条
 * 「今日未签到」，两条积分到期就是两张几乎一样的卡。平铺的后果是一屏全是重复项，
 * 真正该先做什么反而看不见。这里按类别收成一行，账号名退化成 chips。
 */
interface ActionGroup {
  kind: string;
  severity: ActionableItem["severity"];
  title: string;
  details: string[];
  names: string[];
  count: number;
  /** 合并后的动作：可执行项收集全部 accountIds，跳页项沿用第一条。 */
  action: ActionableItem["action"];
}

const SEVERITY_RANK: Record<ActionableItem["severity"], number> = {
  high: 0,
  warn: 1,
  info: 2,
};

/** 从 detail 里取账号名：后端格式是「账号名（平台）：原因」或「账号名：原因」。 */
function nameOf(item: ActionableItem): string {
  return item.detail.split(/[（(：:]/)[0]?.trim() || item.title;
}

const groups = computed<ActionGroup[]>(() => {
  const map = new Map<string, ActionGroup>();
  for (const item of items.value) {
    const found = map.get(item.kind);
    if (!found) {
      map.set(item.kind, {
        kind: item.kind,
        severity: item.severity,
        title: item.title,
        details: [item.detail],
        names: item.accountId ? [nameOf(item)] : [],
        count: 1,
        action: item.action,
      });
      continue;
    }
    found.details.push(item.detail);
    if (item.accountId) found.names.push(nameOf(item));
    found.count += 1;
    if (SEVERITY_RANK[item.severity] < SEVERITY_RANK[found.severity]) {
      found.severity = item.severity;
    }
    // 执行类动作把账号合并进同一批，让「一键执行」一次覆盖整组。
    if (found.action.type === "run" && item.action.type === "run") {
      found.action = {
        type: "run",
        endpoint: found.action.endpoint,
        accountIds: Array.from(new Set([...found.action.accountIds, ...item.action.accountIds])),
        mode: found.action.mode,
        scope: found.action.scope,
      };
    }
  }
  return [...map.values()].sort(
    (a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity] || b.count - a.count,
  );
});

/** 聚合后仍要能说清「一共几个账号受影响」，而不是几个条目。 */
const affectedAccounts = computed(() => {
  const seen = new Set<string>();
  for (const item of items.value) if (item.accountId) seen.add(item.accountId);
  return seen.size;
});

/** 概览只放最靠前的 3 组：再长就变成第二张健康中心，主次反而丢了。 */
const OVERVIEW_GROUP_LIMIT = 3;
const visibleGroups = computed(() => groups.value.slice(0, OVERVIEW_GROUP_LIMIT));
const hiddenGroupCount = computed(() =>
  Math.max(0, groups.value.length - OVERVIEW_GROUP_LIMIT),
);

/** 续期 / 补跑这两类健康中心能自动处理；跳页与启动类不算。 */
function autoHandled(group: ActionGroup): boolean {
  if (group.action.type !== "run") return false;
  return (
    group.action.endpoint === "/credentials/refresh" ||
    group.action.endpoint === "/activities/run"
  );
}
const autoGroupCount = computed(() => groups.value.filter(autoHandled).length);

/**
 * 工作台的核心动作：把所有「可自动处理」的组一次跑完（续期 + 补跑）。
 *
 * 1.12 时概览刻意只读、把执行交给健康中心，避免两个入口各写一份策略。
 * 2.0 把概览定位成工作台：这里只做**执行**，策略仍只在「设置 → 自动化」里改，
 * 所以不构成第二个可写入口。启动客户端不参与批量——它一次只能起一个。
 */
const runnableGroups = computed(() => groups.value.filter(autoHandled));
const runnableAccounts = computed(() => {
  const ids = new Set<string>();
  for (const group of runnableGroups.value) {
    if (group.action.type === "run") for (const id of group.action.accountIds) ids.add(id);
  }
  return ids.size;
});

const batchBusy = ref(false);
const runSummary = ref("");

/** 跑一组可自动项，返回「已处理 / 需人工」两个计数。 */
async function runOne(group: ActionGroup): Promise<{ ok: number; failed: number }> {
  if (group.action.type !== "run") return { ok: 0, failed: 0 };
  if (group.action.endpoint === "/credentials/refresh") {
    const mode =
      group.action.mode === "force" ? "force" : group.action.mode === "check" ? "check" : "due";
    const report = await api.refreshCredentials(mode, group.action.accountIds);
    return {
      ok: report.summary.refreshed + report.summary.ok,
      failed: report.summary.failed + report.summary.reloginRequired,
    };
  }
  if (group.action.endpoint === "/activities/run") {
    const report = await api.runActivities(group.action.accountIds);
    return { ok: report.summary.ok, failed: report.summary.failed };
  }
  return { ok: 0, failed: 0 };
}

/** 一键处理全部可自动项：顺序执行，避免同时打上游触发限流。 */
async function handleAll(): Promise<void> {
  if (batchBusy.value || !runnableGroups.value.length) return;
  batchBusy.value = true;
  runError.value = "";
  runSummary.value = "";
  let ok = 0;
  let failed = 0;
  try {
    for (const group of runnableGroups.value) {
      const result = await runOne(group);
      ok += result.ok;
      failed += result.failed;
    }
    runSummary.value = t("overview.actionables.batchDone", { ok, failed });
    await load();
  } catch (cause) {
    runError.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    batchBusy.value = false;
  }
}

/** 自动处置默认开启后的首次告知：只出现一次，点「知道了」后不再打扰。 */
const AUTO_NOTICE_KEY = "chaierww.health.autoNotice.v1";
const autoNoticeSeen = ref(localStorage.getItem(AUTO_NOTICE_KEY) === "1");

function dismissAutoNotice(): void {
  localStorage.setItem(AUTO_NOTICE_KEY, "1");
  autoNoticeSeen.value = true;
}

async function load(): Promise<void> {
  actionablesLoading.value = true;
  try {
    const next = await api.overviewActionables();
    actionables.value = next.items;
    pulse.value = next.pulse;
    campaigns.value = next.campaigns ?? [];
    loadError.value = "";
  } catch (cause) {
    // 读不到就说读不到，不能渲染成「暂无待处理」——那等于骗用户一切正常。
    loadError.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    actionablesLoading.value = false;
  }
}

async function loadRewards(): Promise<void> {
  rewardsLoading.value = true;
  rewardsError.value = "";
  try {
    rewards.value = await api.activityRewards(7);
  } catch (cause) {
    // 与待办清单同一原则：失败就如实说失败，不渲染成「今天没有收益」。
    rewardsError.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    rewardsLoading.value = false;
  }
}

/** 迷你柱高度：按当周最大值归一；0 也留 2px，免得整排看起来像缺数据。 */
function rewardBarHeight(reward: number): string {
  const days = rewards.value?.daysSeries ?? [];
  const max = Math.max(1, ...days.map((day) => day.reward));
  const ratio = Math.max(reward > 0 ? 0.08 : 0.02, reward / max);
  return `${Math.round(ratio * 100)}%`;
}

function rewardDayLabel(day: string, reward: number): string {
  return `${day} · ${formatAmount(reward)}`;
}

function severityTone(severity: ActionableItem["severity"]) {
  if (severity === "high") return "danger" as const;
  if (severity === "warn") return "warn" as const;
  return "info" as const;
}

function severityLabel(severity: ActionableItem["severity"]): string {
  if (severity === "high") return t("overview.severity.high");
  if (severity === "warn") return t("overview.severity.warn");
  return t("overview.severity.info");
}

function busyFor(group: ActionGroup): boolean {
  return busy.value === group.kind;
}

/** 工作台就地动作：跳页、启动客户端、续期 / 补跑（可自动项）。 */
async function trigger(group: ActionGroup): Promise<void> {
  if (group.action.type === "navigate") {
    await router.push(navTo(group.action.to));
    return;
  }
  if (group.action.type === "run" && autoHandled(group)) {
    busy.value = group.kind;
    runError.value = "";
    runSummary.value = "";
    try {
      const result = await runOne(group);
      runSummary.value = t("overview.actionables.batchDone", {
        ok: result.ok,
        failed: result.failed,
      });
      await load();
    } catch (cause) {
      runError.value = cause instanceof Error ? cause.message : String(cause);
    } finally {
      busy.value = null;
    }
    return;
  }
  if (group.action.endpoint !== "/clients/launch" || !group.action.scope) return;
  busy.value = group.kind;
  runError.value = "";
  try {
    const result = await api.launchClient(group.action.scope);
    if (!result.launched && !result.alreadyRunning) {
      runError.value = result.message;
    } else {
      // 启动是异步的：留一点时间让检测结果翻成「运行中」，否则清单还是旧状态。
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
    await load();
  } catch (cause) {
    runError.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    busy.value = null;
  }
}

/** 组内动作的按钮文案：续期 / 补跑 / 启动，按端点区分。 */
function actionLabel(group: ActionGroup): string {
  if (group.action.type !== "run") return t("overview.action.handle");
  if (group.action.endpoint === "/credentials/refresh") return t("overview.action.refresh");
  if (group.action.endpoint === "/activities/run") return t("overview.action.runActivity");
  return t("overview.action.launch");
}

function toggleClient(scope: string): void {
  const next = new Set(expandedClients.value);
  if (next.has(scope)) next.delete(scope);
  else next.add(scope);
  expandedClients.value = next;
}

onMounted(() => {
  void load();
  void loadRewards();
  void loadGlobalBackups();
  void loadHealthBadge();
  void loadStability();
});

const clientText = computed(() => {
  if (!detection.value) return t("common.unknown");
  if (!detection.value.installed) return t("overview.client.notInstalled");
  return detection.value.running ? t("overview.client.running") : t("overview.client.stopped");
});

const latestSnapshot = computed(() => globalSnapshots.value[0] ?? null);
const runningClients = computed(() => clientDetections.value.filter((item) => item.running).length);

/**
 * 全局备份：设置页「备份」现在只留全局备份，客户端快照已经从界面下线。
 * 概览这里跟着改成读全局备份，KPI 与「最近备份」区块的数字、跳转才和那一页对得上。
 */
const globalSnapshots = ref<Snapshot[]>([]);
const backupsLoading = ref(true);
const backupsError = ref("");

async function loadGlobalBackups(): Promise<void> {
  backupsLoading.value = true;
  backupsError.value = "";
  try {
    const data = await api.globalBackups();
    globalSnapshots.value = data.snapshots;
  } catch (cause) {
    // 读不到就说读不到：不能把失败渲染成 0 / 空态，那等于骗用户「没有备份」。
    backupsError.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    backupsLoading.value = false;
  }
}
</script>

<template>
  <div>
    <PageHeader :title="t('nav.overview')" :subtitle="subtitle">
      <template #actions>
        <RouterLink
          :to="navTo('/accounts')"
          class="inline-flex shrink-0 items-center gap-1.5 rounded-control border border-line bg-surface px-3 py-2 text-body-sm text-ink-soft transition-colors hover:border-line-strong hover:text-ink"
        >
          <Users :size="14" :stroke-width="1.9" /> {{ t("overview.manageAccounts") }}
        </RouterLink>
      </template>
    </PageHeader>

    <OnboardingChecklist class="mt-4" />

    <!-- 自动处置回执：默认开启后，让用户看到它确实在干活，而不是只在设置里有个开关。 -->
    <div
      v-if="healthAutoStats && (healthAutoStats.ok24h || healthAutoStats.failed24h)"
      class="mt-4 flex flex-wrap items-center gap-x-2 gap-y-1 rounded-card border border-line bg-surface px-4 py-2.5 text-body-sm text-ink-soft"
    >
      <ShieldCheck :size="15" :stroke-width="1.9" class="shrink-0 text-brand" />
      <span class="min-w-0 flex-1">
        {{ t("overview.health.auto", {
          ok: healthAutoStats.ok24h,
          failed: healthAutoStats.failed24h,
        }) }}
        <template v-if="!autoNoticeSeen">
          {{ t("overview.health.autoNotice") }}
          <button
            type="button"
            class="ml-1 text-brand-dark hover:underline"
            @click="dismissAutoNotice"
          >
            {{ t("switch.nextStep.dismiss") }}
          </button>
        </template>
      </span>
      <RouterLink
        to="/health"
        class="shrink-0 text-caption text-brand-dark hover:underline"
      >
        {{ t("overview.health.cta") }}
      </RouterLink>
    </div>

    <!-- 结论条：一眼回答「今天要不要管」，右侧直达对应页面。 -->
    <div
      class="mt-5 flex flex-wrap items-center gap-x-2 gap-y-1 rounded-card border px-4 py-3 text-body-sm"
      :class="
        conclusion.tone === 'warn'
          ? 'border-warn/30 bg-warn-soft text-warn'
          : 'border-line bg-surface text-ink-soft'
      "
    >
      <component
        :is="conclusion.icon"
        :size="16"
        :stroke-width="1.9"
        class="shrink-0"
        :class="conclusion.tone === 'warn' ? 'text-warn' : 'text-brand'"
      />
      <span class="min-w-0 flex-1">
        {{ conclusion.text }}
        <span v-if="unstableCount" class="ml-1 text-caption">
          {{ t("overview.conclusion.stability", { count: unstableCount }) }}
        </span>
      </span>
      <RouterLink
        :to="conclusion.to"
        class="shrink-0 text-caption hover:underline"
        :class="conclusion.tone === 'warn' ? 'text-warn' : 'text-brand-dark'"
      >
        {{ conclusion.cta }}
      </RouterLink>
    </div>

    <div class="metric-ribbon mt-5">
      <StatCard :label="t('overview.stat.accounts')" :number="accounts.length" :icon="Users" :hint="t('overview.stat.accountsHint')" :to="navTo('/accounts')" />
      <StatCard
        :label="t('overview.stat.credentialIssue')"
        :number="attentionCount"
        :icon="AlertTriangle"
        :tone="attentionCount ? 'warn' : undefined"
        :hint="
          attentionCount
            ? t('overview.stat.credentialIssueSplit', {
                relogin: credentialIssueCounts.relogin,
                expired: credentialIssueCounts.expired,
                expiring: credentialIssueCounts.expiring,
              })
            : t('overview.stat.allGood')
        "
        :to="navTo('/accounts?risk=credential')"
      />
      <StatCard
        :label="t('overview.stat.snapshots')"
        :number="backupsError || backupsLoading ? undefined : globalSnapshots.length"
        :value="backupsError ? '—' : backupsLoading ? '…' : undefined"
        :icon="Database"
        :hint="
          backupsError
            ? t('overview.stat.backupsUnavailable')
            : backupsLoading
              ? t('common.loading')
              : latestSnapshot
                ? t('overview.stat.lastBackup', { time: formatTime(latestSnapshot.createdAt) })
                : t('overview.stat.noBackups')
        "
      />
      <StatCard
        :label="t('overview.stat.clientsRunning')"
        :number="runningClients"
        :icon="MonitorSmartphone"
        :hint="t('overview.client.runningCount', { running: runningClients, total: clientDetections.length })"
        :to="navTo('/accounts')"
      />
    </div>

    <p v-if="runError" class="mt-3 rounded-card border border-danger/30 bg-danger-soft px-4 py-2.5 text-body-sm text-danger">
      {{ runError }}
    </p>
    <p
      v-else-if="runSummary"
      class="mt-3 rounded-card border border-brand/30 bg-brand-soft px-4 py-2.5 text-body-sm text-brand-dark"
    >
      {{ runSummary }}
    </p>

    <div class="page-hero mt-4">
    <SectionCard
      :title="t('overview.actionables.title')"
      :subtitle="
        groups.length
          ? autoGroupCount
            ? t('overview.actionables.summaryAuto', {
                groups: groups.length,
                accounts: affectedAccounts,
                auto: autoGroupCount,
              })
            : t('overview.actionables.summary', { groups: groups.length, accounts: affectedAccounts })
          : undefined
      "
      flush
    >
      <template #actions>
        <button
          v-if="runnableAccounts"
          type="button"
          class="inline-flex items-center gap-1.5 rounded-control bg-brand px-3 py-1.5 text-body-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-60"
          :disabled="batchBusy"
          @click="handleAll"
        >
          <Loader2 v-if="batchBusy" :size="12" class="animate-spin" />
          {{
            batchBusy
              ? t("overview.actionables.batching")
              : t("overview.actionables.batch", { count: runnableAccounts })
          }}
        </button>
      </template>
      <p v-if="loadError" class="px-5 py-3.5 text-body-sm text-danger">
        {{ t("overview.actionables.error", { message: loadError }) }}
      </p>
      <div v-else-if="actionablesLoading" class="space-y-1 px-5 py-2">
        <Skeleton variant="row" :lines="2" />
        <Skeleton variant="row" :lines="2" />
      </div>
      <div
        v-else-if="!groups.length"
        class="flex flex-wrap items-center gap-2.5 px-5 py-8 text-body text-ink-soft"
      >
        <template v-if="healthUnhandled">
          <HeartPulse :size="16" class="text-warn" />
          {{ t("overview.health.title", { count: healthUnhandled }) }}
          <RouterLink :to="navTo('/health')" class="font-medium text-brand-dark hover:underline">
            {{ t("overview.health.cta") }}
          </RouterLink>
        </template>
        <template v-else>
          <CheckCircle2 :size="16" class="text-brand" />
          {{ t("overview.actionables.empty") }}
        </template>
      </div>
      <div v-else>
        <ul class="divide-y divide-line-soft">
          <li v-for="group in visibleGroups" :key="group.kind" class="flex items-start gap-3 px-5 py-3.5">
            <AlertTriangle
              :size="15"
              class="mt-0.5 shrink-0"
              :class="group.severity === 'info' ? 'text-muted' : 'text-warn'"
            />
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <span class="text-body font-medium text-ink">{{ group.title }}</span>
                <span v-if="group.count > 1" class="text-caption text-muted">
                  {{ t("overview.actionables.accounts", { count: group.count }) }}
                </span>
              </div>
              <p
                v-if="group.count === 1"
                class="mt-0.5 text-body-sm leading-relaxed text-muted"
              >
                {{ group.details[0] }}
              </p>
              <div v-else class="mt-1.5 flex flex-wrap items-center gap-1.5">
                <span
                  v-for="(name, index) in group.names.slice(0, 6)"
                  :key="`${group.kind}-${index}`"
                  class="rounded-full border border-line-soft bg-line-soft px-2 py-[1px] text-caption text-ink-soft"
                  :title="group.details[index]"
                >
                  {{ name }}
                </span>
                <span
                  v-if="group.names.length > 6"
                  class="px-1 text-caption text-muted"
                  :title="group.details.slice(6).join('\n')"
                >
                  +{{ group.names.length - 6 }}
                </span>
              </div>
            </div>
            <StatusPill :tone="severityTone(group.severity)" class="mt-0.5 shrink-0">
              {{ severityLabel(group.severity) }}
            </StatusPill>
            <button
              v-if="
                group.action.type === 'run' &&
                (autoHandled(group) || group.action.endpoint === '/clients/launch')
              "
              type="button"
              class="inline-flex shrink-0 items-center gap-1 rounded-control border border-line px-2.5 py-1.5 text-body-sm text-ink-soft transition-colors hover:border-line-strong hover:text-ink disabled:opacity-50"
              :disabled="busyFor(group) || batchBusy"
              @click="trigger(group)"
            >
              <Loader2 v-if="busyFor(group)" :size="11" class="animate-spin" />
              <template v-else>{{ actionLabel(group) }} <ArrowRight :size="11" /></template>
            </button>
            <button
              v-else-if="group.action.type === 'navigate'"
              type="button"
              class="inline-flex shrink-0 items-center gap-1 rounded-control border border-line px-2.5 py-1.5 text-body-sm text-ink-soft transition-colors hover:border-line-strong hover:text-ink"
              @click="trigger(group)"
            >
              {{ t("overview.action.handle") }} <ArrowRight :size="11" />
            </button>
            <RouterLink
              v-else
              :to="navTo('/health')"
              class="inline-flex shrink-0 items-center gap-1 rounded-control border border-line px-2.5 py-1.5 text-body-sm text-brand-dark transition-colors hover:border-brand/50"
            >
              {{ t("overview.health.cta") }}
            </RouterLink>
          </li>
        </ul>
        <RouterLink
          :to="navTo('/health')"
          class="flex items-center justify-center gap-1 border-t border-line-soft px-5 py-2.5 text-body-sm text-brand-dark transition-colors hover:bg-line-soft/40"
        >
          {{
            hiddenGroupCount
              ? t("overview.actionables.viewAll", { count: hiddenGroupCount })
              : t("overview.health.cta")
          }}
          <ArrowRight :size="13" :stroke-width="2" />
        </RouterLink>
      </div>
    </SectionCard>

    <!-- 系统状态窄栏：只讲账号维度的事实，供给 / 上游细节统一去健康中心看。 -->
    <aside class="status-aside">
      <div class="rounded-card border border-line bg-surface shadow-card">
        <div class="border-b border-line-soft px-4 py-2.5 text-body-sm font-medium text-ink">
          {{ t("overview.system.title") }}
        </div>
        <dl class="divide-y divide-line-soft px-4 text-body-sm">
          <div class="flex items-center justify-between gap-3 py-2.5">
            <dt class="text-muted">{{ t("overview.system.currentAccount") }}</dt>
            <dd class="min-w-0 truncate font-medium text-ink">
              {{ currentAccount?.displayName ?? t("overview.system.noAccount") }}
            </dd>
          </div>
          <div class="flex items-center justify-between gap-3 py-2.5">
            <dt class="text-muted">{{ t("overview.system.accounts") }}</dt>
            <dd class="font-ledger font-medium tabular-nums text-ink">{{ accounts.length }}</dd>
          </div>
          <div class="flex items-center justify-between gap-3 py-2.5">
            <dt class="text-muted">{{ t("overview.system.unhandled") }}</dt>
            <dd
              class="font-ledger font-medium tabular-nums"
              :class="healthUnhandled ? 'text-warn' : 'text-ink'"
            >
              {{ healthUnhandled }}
            </dd>
          </div>
          <div class="flex items-center justify-between gap-3 py-2.5">
            <dt class="text-muted">{{ t("overview.system.lastBackup") }}</dt>
            <dd class="min-w-0 truncate text-right font-medium text-ink">
              <template v-if="backupsError">—</template>
              <template v-else-if="backupsLoading">…</template>
              <template v-else-if="latestSnapshot">
                {{ formatTime(latestSnapshot.createdAt) }}
              </template>
              <template v-else>{{ t("overview.system.neverBackup") }}</template>
            </dd>
          </div>
        </dl>
        <RouterLink
          :to="navTo('/health')"
          class="flex items-center justify-center gap-1 border-t border-line-soft px-4 py-2.5 text-body-sm text-brand-dark transition-colors hover:bg-line-soft/40"
        >
          {{ t("overview.system.openHealth") }}
          <ArrowRight :size="13" :stroke-width="2" />
        </RouterLink>
      </div>
    </aside>
    </div>

    <OfficialActivityStrip v-if="pulse && campaigns.length" class="mt-4" :campaigns="campaigns" />

    <!-- 第四个内容块：今日近况。收益 / 用量 / 客户端都在这里，明细到对应页面看。 -->
    <div class="mb-2 mt-6 flex flex-wrap items-baseline gap-x-2 gap-y-1">
      <h3 class="text-title font-semibold tracking-tight text-ink">
        {{ t("overview.today.title") }}
      </h3>
      <p class="text-caption text-muted">{{ t("overview.today.hint") }}</p>
    </div>

    <!-- 活动收益：口径只含本工具代做记录，手动领取不计，卡片上写明避免当成账号总收益 -->
    <SectionCard class="mt-2" :title="t('overview.rewards.title')" :subtitle="t('overview.rewards.hint')">
      <div v-if="rewardsLoading" class="space-y-1">
        <Skeleton variant="row" :lines="2" />
      </div>
      <div
        v-else-if="rewardsError"
        class="flex flex-wrap items-center gap-2 text-body-sm text-danger"
      >
        <span class="min-w-0 flex-1">{{ t("overview.rewards.error", { message: rewardsError }) }}</span>
        <button
          type="button"
          class="inline-flex shrink-0 items-center rounded-badge border border-line px-2 py-1 text-caption text-ink-soft transition-colors hover:border-line-strong hover:text-ink"
          @click="loadRewards"
        >
          {{ t("common.retry") }}
        </button>
      </div>
      <div v-else-if="!rewards || (!rewards.total && !rewards.byAccount.length)" class="text-body-sm text-muted">
        {{ t("overview.rewards.empty") }}
      </div>
      <div v-else class="flex flex-wrap items-end gap-x-6 gap-y-3">
        <div>
          <div class="text-caption text-muted">{{ t("overview.rewards.today") }}</div>
          <div class="mt-0.5 font-ledger text-metric font-semibold leading-none text-ink">
            {{ formatAmount(rewards.today) }}
          </div>
        </div>
        <div>
          <div class="text-caption text-muted">{{
            t("overview.rewards.week", { days: rewards.days })
          }}</div>
          <div class="mt-0.5 text-title font-semibold leading-none text-ink">
            {{ formatAmount(rewards.total) }}
          </div>
        </div>

        <!-- 近 7 天迷你柱：高度按当周最大值归一，悬停看当天数字 -->
        <div class="ml-auto flex h-12 items-end gap-[3px]">
          <span
            v-for="day in rewards.daysSeries"
            :key="day.day"
            class="w-[7px] rounded-full bg-brand/70 transition-colors hover:bg-brand"
            :style="{ height: rewardBarHeight(day.reward) }"
            :title="rewardDayLabel(day.day, day.reward)"
          />
        </div>

        <RouterLink
          v-for="row in rewards.byAccount.slice(0, 3)"
          :key="row.accountId"
          :to="navTo(`/accounts?account=${row.accountId}`)"
          class="inline-flex items-center gap-1.5 rounded-full border border-line-soft bg-line-soft px-2.5 py-1 text-caption text-ink-soft transition-colors hover:border-brand/40 hover:text-brand-dark"
          :title="t('overview.rewards.accountHint', { name: row.displayName })"
        >
          <span class="max-w-[7rem] truncate">{{ row.displayName }}</span>
          <span class="shrink-0 font-medium tabular-nums">{{ formatAmount(row.reward) }}</span>
        </RouterLink>
      </div>
    </SectionCard>

    <div class="mt-3 grid gap-4 lg:grid-cols-[1.35fr_1fr]">
      <SectionCard
        :title="t('overview.client.title')"
        :subtitle="
          clientDetections.length
            ? t('overview.client.runningCount', { running: runningClients, total: clientDetections.length })
            : undefined
        "
      >
        <div v-if="!clientDetections.length" class="space-y-2.5 text-body-sm">
          <div class="flex items-center justify-between">
            <span class="text-muted">{{ t("overview.client.status") }}</span>
            <StatusPill :tone="detection?.running ? 'warn' : 'brand'" dot>{{ clientText }}</StatusPill>
          </div>
          <div class="flex items-center justify-between">
            <span class="text-muted">{{ t("overview.client.login") }}</span>
            <StatusPill :tone="detection?.loggedIn ? 'brand' : 'neutral'">
              {{ detection?.loggedIn ? t("overview.client.loggedIn") : t("overview.client.loggedOut") }}
            </StatusPill>
          </div>
          <div v-if="detection?.stateDir" class="pt-1">
            <div class="text-muted">{{ t("overview.client.stateDir") }}</div>
            <div class="mt-1 break-all rounded-control bg-line-soft px-2 py-1.5 font-mono text-caption text-ink-soft">
              {{ detection.stateDir }}
            </div>
          </div>
        </div>

        <ul v-else class="divide-y divide-line-soft">
          <li v-for="client in clientDetections" :key="client.scope" class="py-2.5 first:pt-0 last:pb-0">
            <div class="flex items-center gap-2">
              <span class="min-w-0 flex-1 truncate text-body-sm font-medium text-ink">
                {{ client.label }}
              </span>
              <StatusPill :tone="client.running ? 'warn' : 'neutral'" dot>
                {{ client.running ? t("overview.client.running") : t("overview.client.stopped") }}
              </StatusPill>
              <StatusPill :tone="client.loggedIn ? 'brand' : 'neutral'">
                {{ client.loggedIn ? t("overview.client.loggedIn") : t("overview.client.loggedOut") }}
              </StatusPill>
            </div>
            <div v-if="client.stateDir" class="mt-1">
              <button
                type="button"
                class="flex w-full items-center gap-1 text-left"
                :title="client.stateDir"
                @click="toggleClient(client.scope)"
              >
                <span
                  class="min-w-0 flex-1 font-mono text-micro text-muted"
                  :class="expandedClients.has(client.scope) ? 'break-all' : 'truncate'"
                >
                  {{ client.stateDir }}
                </span>
                <span class="shrink-0 text-micro text-muted hover:text-ink">
                  {{ expandedClients.has(client.scope) ? t("overview.client.collapse") : t("overview.client.expand") }}
                </span>
              </button>
            </div>
            <div v-if="client.message" class="mt-1 text-caption text-muted">{{ client.message }}</div>
          </li>
        </ul>
      </SectionCard>

      <SectionCard :title="t('overview.pulse.title')" :subtitle="t('overview.pulse.subtitle')">
        <div v-if="!pulse" class="text-body-sm text-muted">
          {{ t("overview.pulse.loading") }}
        </div>
        <div v-else class="space-y-2">
          <RouterLink
            :to="navTo('/activity')"
            class="flex items-center gap-3 rounded-control px-2.5 py-2 transition-colors hover:bg-line-soft"
          >
            <Activity :size="14" class="shrink-0 text-muted" />
            <span class="min-w-0 flex-1 truncate text-body-sm text-muted">{{ t("overview.pulse.activity") }}</span>
            <!-- 双口径：项数对应「一键执行」的量，账号数对应「要处理几个号」 -->
            <span class="shrink-0 text-body font-medium tabular-nums text-ink">
              {{ t("overview.pulse.activityPending", {
                items: pulse.activityActionable,
                accounts: pulse.activityActionableAccounts,
              }) }}
            </span>
            <span class="shrink-0 text-caption text-muted">
              {{ t("overview.pulse.activityShort", { done: pulse.activityDone, total: pulse.activityTotal }) }}
            </span>
          </RouterLink>
          <RouterLink
            :to="navTo('/usage')"
            class="flex items-center gap-3 rounded-control px-2.5 py-2 transition-colors hover:bg-line-soft"
          >
            <Coins :size="14" class="shrink-0 text-muted" />
            <span class="min-w-0 flex-1 truncate text-body-sm text-muted">{{ t("overview.pulse.credits") }}</span>
            <span class="shrink-0 text-body font-medium tabular-nums text-ink">{{ formatAmount(pulse.credits) }}</span>
            <span
              class="shrink-0 text-caption"
              :class="pulse.creditsExpiring ? 'text-warn' : 'text-muted'"
            >
              {{ t("overview.pulse.creditsShort", { count: pulse.creditsExpiring }) }}
            </span>
          </RouterLink>
          <RouterLink
            :to="navTo('/gateway')"
            class="flex items-center gap-3 rounded-control px-2.5 py-2 transition-colors hover:bg-line-soft"
          >
            <Network :size="14" class="shrink-0 text-muted" />
            <span class="min-w-0 flex-1 truncate text-body-sm text-muted">{{ t("overview.pulse.gateway") }}</span>
            <span class="shrink-0 text-body font-medium tabular-nums text-ink">
              {{ pulse.gateway.requests }}
            </span>
            <span class="shrink-0 text-caption text-muted">
              {{ t("overview.pulse.gatewayShort", { keys: pulse.gateway.activeKeys, errors: pulse.gateway.errors }) }}
            </span>
          </RouterLink>
        </div>
      </SectionCard>
    </div>
  </div>
</template>

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
  Loader2,
  MonitorSmartphone,
  Network,
  Repeat2,
  Users,
} from "lucide-vue-next";
import ConfirmDialog from "../components/ConfirmDialog.vue";
import SectionCard from "../components/SectionCard.vue";
import StatCard from "../components/StatCard.vue";
import StatusPill from "../components/StatusPill.vue";
import OfficialActivityStrip from "../components/OfficialActivityStrip.vue";
import { api, formatAmount, formatBytes, formatTime, type ActionableItem, type ActivityCampaign, type OverviewPulse, type Snapshot } from "../api";
import { accounts, attentionCount, clientDetections, detection, scopedPath } from "../stores/app";
import { acknowledgeGrowthNotice, growthNoticeAcknowledged } from "../lib/growthNotice";
import { t } from "../lib/i18n";

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
const loadError = ref("");
const busy = ref<string | null>(null);
/** 客户端登录态目录默认收成一行，点开才铺完整路径 —— 它不是常用信息。 */
const expandedClients = ref<Set<string>>(new Set());

/** 页面本地的引导态：这些不属于「号池待处理」，但同样挡着用户。 */
const onboarding = computed<ActionableItem[]>(() => {
  const list: ActionableItem[] = [];
  const detect = detection.value;
  if (detect && !detect.installed) {
    list.push({
      kind: "onboarding",
      severity: "info",
      title: t("state.clientMissing"),
      detail: t("overview.onboarding.clientDetail"),
      accountId: null,
      action: { type: "navigate", to: "/accounts" },
    });
  }
  if (detect?.installed && !detect.loggedIn) {
    list.push({
      kind: "onboarding",
      severity: "info",
      title: t("overview.clientLoggedOut"),
      detail: t("overview.onboarding.loggedOutDetail"),
      accountId: null,
      action: { type: "navigate", to: "/accounts" },
    });
  }
  if (!accounts.value.length) {
    list.push({
      kind: "onboarding",
      severity: "info",
      title: t("overview.onboarding.noAccounts"),
      detail: t("overview.onboarding.noAccountsDetail"),
      accountId: null,
      action: { type: "navigate", to: "/accounts" },
    });
  }
  return list;
});

const items = computed<ActionableItem[]>(() => [...onboarding.value, ...actionables.value]);

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

async function load(): Promise<void> {
  try {
    const next = await api.overviewActionables();
    actionables.value = next.items;
    pulse.value = next.pulse;
    campaigns.value = next.campaigns ?? [];
    loadError.value = "";
  } catch (cause) {
    // 读不到就说读不到，不能渲染成「暂无待处理」——那等于骗用户一切正常。
    loadError.value = cause instanceof Error ? cause.message : String(cause);
  }
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

function actionLabel(group: ActionGroup): string {
  if (group.action.type === "run") return t("overview.action.run");
  if (group.kind === "onboarding") return t("overview.action.accounts");
  return t("overview.action.handle");
}

function busyFor(group: ActionGroup): boolean {
  return busy.value === group.kind;
}

/** 待确认的活动执行：成长任务首次执行前先做风险告知（见 `lib/growthNotice`）。 */
const pendingRun = ref<ActionGroup | null>(null);

/** 按 action.type 分发：跳页交给 router，执行类调用对应接口后静默刷新清单。 */
async function trigger(group: ActionGroup): Promise<void> {
  if (group.action.type === "navigate") {
    await router.push(navTo(group.action.to));
    return;
  }
  if (group.action.endpoint === "/activities/run" && !growthNoticeAcknowledged()) {
    pendingRun.value = group;
    return;
  }
  await runGroup(group);
}

function cancelRunNotice(): void {
  pendingRun.value = null;
}

async function confirmRunNotice(): Promise<void> {
  const group = pendingRun.value;
  pendingRun.value = null;
  if (!group) return;
  acknowledgeGrowthNotice();
  await runGroup(group);
}

async function runGroup(group: ActionGroup): Promise<void> {
  const action = group.action;
  // 跳页类在一开始就分流了，走到这里的只可能是执行类；显式守住类型。
  if (action.type !== "run") return;
  busy.value = group.kind;
  try {
    if (action.endpoint === "/activities/run") {
      await api.runActivities(action.accountIds);
    } else if (action.endpoint === "/credentials/refresh") {
      const mode = (action.mode as "due" | "force" | "check" | undefined) ?? "due";
      await api.refreshCredentials(mode, action.accountIds);
    }
    await load();
  } catch (cause) {
    loadError.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    busy.value = null;
  }
}

function toggleClient(scope: string): void {
  const next = new Set(expandedClients.value);
  if (next.has(scope)) next.delete(scope);
  else next.add(scope);
  expandedClients.value = next;
}

onMounted(() => {
  void load();
  void loadGlobalBackups();
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

async function loadGlobalBackups(): Promise<void> {
  try {
    const data = await api.globalBackups();
    globalSnapshots.value = data.snapshots;
  } catch {
    // 读不到就留空，KPI 显示 0、区块显示空态，不冒充「没有备份」。
    globalSnapshots.value = [];
  }
}
</script>

<template>
  <div>
    <div class="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
      <div class="min-w-0">
        <h1 class="text-display font-semibold tracking-[-0.02em] text-ink">{{ t("nav.overview") }}</h1>
        <p class="mt-1.5 max-w-[68ch] text-body leading-relaxed text-muted">{{ subtitle }}</p>
      </div>
      <RouterLink
        :to="navTo('/accounts')"
        class="inline-flex shrink-0 items-center gap-1.5 rounded-control border border-line bg-surface px-3 py-2 text-body-sm text-ink-soft transition-colors hover:border-line-strong hover:text-ink"
      >
        <Users :size="14" :stroke-width="1.9" /> {{ t("overview.manageAccounts") }}
      </RouterLink>
    </div>

    <div class="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <StatCard :label="t('overview.stat.accounts')" :number="accounts.length" :icon="Users" :hint="t('overview.stat.accountsHint')" :to="navTo('/accounts')" />
      <StatCard
        :label="t('overview.stat.credentialIssue')"
        :number="attentionCount"
        :icon="AlertTriangle"
        :tone="attentionCount ? 'warn' : undefined"
        :hint="attentionCount ? t('overview.stat.credentialIssueHint') : t('overview.stat.allGood')"
        :to="navTo('/accounts?risk=relogin')"
      />
      <StatCard
        :label="t('overview.stat.snapshots')"
        :number="globalSnapshots.length"
        :icon="Database"
        :hint="
          latestSnapshot
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

    <SectionCard
      class="mt-4"
      :title="t('overview.actionables.title')"
      :subtitle="
        groups.length
          ? t('overview.actionables.summary', { groups: groups.length, accounts: affectedAccounts })
          : undefined
      "
      flush
    >
      <p v-if="loadError" class="px-5 py-3.5 text-body-sm text-danger">
        {{ t("overview.actionables.error", { message: loadError }) }}
      </p>
      <div
        v-else-if="!groups.length"
        class="flex items-center gap-2.5 px-5 py-8 text-body text-ink-soft"
      >
        <CheckCircle2 :size="16" class="text-brand" />
        {{ t("overview.actionables.empty") }}
      </div>
      <ul v-else class="divide-y divide-line-soft">
        <li v-for="group in groups" :key="group.kind" class="flex items-start gap-3 px-5 py-3.5">
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
            type="button"
            class="inline-flex shrink-0 items-center gap-1 rounded-control border border-line px-2.5 py-1.5 text-body-sm text-ink-soft transition-colors hover:border-line-strong hover:text-ink disabled:opacity-50"
            :disabled="busyFor(group)"
            @click="trigger(group)"
          >
            <Loader2 v-if="busyFor(group)" :size="11" class="animate-spin" />
            <template v-else>{{ actionLabel(group) }} <ArrowRight :size="11" /></template>
          </button>
        </li>
      </ul>
    </SectionCard>

    <OfficialActivityStrip v-if="pulse" class="mt-4" :campaigns="campaigns" />

    <div class="mt-4 grid gap-4 lg:grid-cols-[1.35fr_1fr]">
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
            <span class="shrink-0 text-body font-medium tabular-nums text-ink">
              {{ pulse.activityActionable }}
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

    <div class="mt-4">
      <SectionCard :title="t('overview.snapshots.title')" :subtitle="t('overview.snapshots.subtitle')" flush>
        <template #actions>
          <RouterLink :to="navTo('/settings?tab=backup')" class="text-body-sm text-muted transition-colors hover:text-ink">
            {{ t("overview.snapshots.viewAll") }}
          </RouterLink>
        </template>
        <div v-if="!globalSnapshots.length" class="px-5 py-8 text-center text-body text-muted">
          {{ t("overview.snapshots.empty") }}
        </div>
        <ul v-else class="divide-y divide-line-soft">
          <li
            v-for="snapshot in globalSnapshots.slice(0, 2)"
            :key="snapshot.id"
            class="flex items-center gap-4 px-5 py-3 text-body-sm"
          >
            <Repeat2 :size="14" class="shrink-0 text-muted" />
            <span class="w-20 shrink-0 text-ink-soft">
              {{
                snapshot.reason === "manual"
                  ? t("overview.snapshots.manual")
                  : t("overview.snapshots.auto")
              }}
            </span>
            <span class="min-w-0 flex-1 text-muted">{{ formatTime(snapshot.createdAt) }}</span>
            <span class="shrink-0 text-muted">
              {{ t("overview.snapshots.files", { count: snapshot.fileCount }) }} · {{ formatBytes(snapshot.totalBytes) }}
            </span>
            <StatusPill :tone="snapshot.verified ? 'brand' : 'warn'">
              {{ snapshot.verified ? t("overview.snapshots.verified") : t("overview.snapshots.unverified") }}
            </StatusPill>
          </li>
        </ul>
      </SectionCard>
    </div>

    <ConfirmDialog
      :open="pendingRun !== null"
      :title="t('activity.noticeTitle')"
      :message="t('activity.noticeBody')"
      :confirm-text="t('activity.noticeConfirm')"
      @confirm="confirmRunNotice"
      @cancel="cancelRunNotice"
    />
  </div>
</template>

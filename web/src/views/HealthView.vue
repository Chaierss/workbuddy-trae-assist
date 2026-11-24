<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  Clock3,
  Coins,
  History,
  Loader2,
  Network,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-vue-next";
import PageHeader from "../components/PageHeader.vue";
import Skeleton from "../components/Skeleton.vue";
import StatusPill from "../components/StatusPill.vue";
import {
  api,
  formatTime,
  type CompatItem,
  type CompatOverview,
  type HealthAction,
  type HealthActionLog,
  type HealthEvent,
  type HealthEventState,
  type HealthSeverity,
  type HealthSnapshot,
  type PoolHealth,
} from "../api";
import { healthUnhandled } from "../stores/app";
import { t, type MessageKey } from "../lib/i18n";

/**
 * 健康中心：把 1.7 铺开的四类信号（上游结构 / 登录 / 供给 / 活动）与计划任务失败
 * 收成一条事件流。
 *
 * 页面只做两件事：把「哪件事需要处理」按严重度摊开；把「怎么处理」交给服务端
 * 登记的动作白名单 —— 前端不拼接口，也不自己判断该续期还是该重登。
 */
const router = useRouter();
const route = useRoute();

const snapshot = ref<HealthSnapshot | null>(null);
const filter = ref<"active" | "resolved" | "all">("active");
const kindFilter = ref("all");
const accountFilter = ref("");
const groupFilter = ref("");
const autoOnly = ref(false);
const loading = ref(true);
const error = ref("");
const busy = ref<string | null>(null);
const selected = ref<Set<string>>(new Set());
const auditOpen = ref<string | null>(null);
const audits = ref<Record<string, HealthActionLog[]>>({});
const flash = ref<{ text: string; tone: "ok" | "bad" } | null>(null);

/** 系统基线：号池供给与上游结构。数据只读，读不到就整块不显示。 */
const poolHealth = ref<PoolHealth | null>(null);
const compat = ref<CompatOverview | null>(null);
const compatBusy = ref(false);

const COMPAT_LABEL: Record<CompatItem["state"], MessageKey> = {
  compatible: "overview.compat.compatible",
  changed: "overview.compat.changed",
  missing: "overview.compat.missing",
  unknown: "overview.compat.unknown",
  not_probed: "overview.compat.notProbed",
};

const COMPAT_TONE: Record<CompatItem["state"], "brand" | "warn" | "danger" | "neutral"> = {
  compatible: "brand",
  changed: "warn",
  missing: "danger",
  unknown: "neutral",
  not_probed: "neutral",
};

function supplyDaysText(value: number | null): string {
  return value === null ? t("overview.supply.noData") : t("overview.supply.days", { days: value });
}

function supplyRateText(value: number | null): string {
  return value === null
    ? t("overview.supply.noData")
    : t("overview.supply.rate", { percent: Math.round(value * 100) });
}

async function loadBaseline(): Promise<void> {
  const [supply, compatResult] = await Promise.allSettled([
    api.poolHealth(),
    api.compat(),
  ]);
  if (supply.status === "fulfilled") poolHealth.value = supply.value;
  if (compatResult.status === "fulfilled") compat.value = compatResult.value;
}

async function probeUpstream(): Promise<void> {
  if (compatBusy.value) return;
  compatBusy.value = true;
  try {
    compat.value = await api.probeCompat();
  } catch {
    // 探测失败保留旧结果：基线不是关键路径。
  } finally {
    compatBusy.value = false;
  }
}

const KIND_LABEL: Record<string, MessageKey> = {
  compat_changed: "health.kind.compat_changed",
  login_invalid: "health.kind.login_invalid",
  login_blocked: "health.kind.login_blocked",
  login_expiring: "health.kind.login_expiring",
  credential_expired: "health.kind.credential_expired",
  credential_expiring: "health.kind.credential_expiring",
  credential_stalled: "health.kind.credential_stalled",
  credential_relogin: "health.kind.credential_relogin",
  pool_supply: "health.kind.pool_supply",
  activity_failed: "health.kind.activity_failed",
  schedule_failed: "health.kind.schedule_failed",
};

const KIND_OPTIONS = Object.keys(KIND_LABEL);

/**
 * 深链分组：概览 / 号池只负责「指路」，不再就地提供第二套处置按钮。
 * 地址形如 `/health?group=credential` 或 `/health?account=<id>`。
 */
const GROUP_KINDS: Record<string, string[]> = {
  credential: [
    "login_invalid",
    "login_blocked",
    "login_expiring",
    "credential_expired",
    "credential_expiring",
    "credential_stalled",
    "credential_relogin",
  ],
  activity: ["activity_failed"],
  supply: ["pool_supply"],
  compat: ["compat_changed"],
  schedule: ["schedule_failed"],
};

const GROUP_LABEL: Record<string, MessageKey> = {
  credential: "health.filter.group.credential",
  activity: "health.filter.group.activity",
  supply: "health.filter.group.supply",
  compat: "health.filter.group.compat",
  schedule: "health.filter.group.schedule",
};

function applyRouteFilters(): void {
  const group = typeof route.query.group === "string" ? route.query.group : "";
  groupFilter.value = GROUP_KINDS[group] ? group : "";
  const kind = typeof route.query.kind === "string" ? route.query.kind : "";
  kindFilter.value = KIND_OPTIONS.includes(kind) ? kind : "all";
  accountFilter.value = typeof route.query.account === "string" ? route.query.account : "";
}

applyRouteFilters();
watch(() => [route.query.group, route.query.kind, route.query.account], applyRouteFilters);

/** 用户改筛选时同步回地址栏：刷新、收藏、分享都能带回同一个视图。 */
watch([kindFilter, accountFilter, groupFilter], () => {
  const query = { ...route.query };
  if (groupFilter.value) query.group = groupFilter.value;
  else delete query.group;
  if (kindFilter.value !== "all") query.kind = kindFilter.value;
  else delete query.kind;
  if (accountFilter.value) query.account = accountFilter.value;
  else delete query.account;
  if (
    query.group === route.query.group &&
    query.kind === route.query.kind &&
    query.account === route.query.account
  ) {
    return;
  }
  void router.replace({ path: route.path, query });
});

const SEVERITY_TONE: Record<HealthSeverity, "danger" | "warn" | "info"> = {
  high: "danger",
  warn: "warn",
  info: "info",
};

const STATE_TONE: Record<HealthEventState, "brand" | "warn" | "neutral"> = {
  open: "brand",
  acked: "neutral",
  snoozed: "warn",
  resolved: "neutral",
};

const FILTERS: Array<{ key: "active" | "resolved" | "all"; labelKey: MessageKey }> = [
  { key: "active", labelKey: "health.filter.active" },
  { key: "resolved", labelKey: "health.filter.resolved" },
  { key: "all", labelKey: "health.filter.all" },
];

const SNOOZE_OPTIONS: Array<{ hours: number; labelKey: MessageKey }> = [
  { hours: 1, labelKey: "health.snooze.1h" },
  { hours: 24, labelKey: "health.snooze.24h" },
  { hours: 72, labelKey: "health.snooze.72h" },
];

const summary = computed(() => snapshot.value?.summary ?? null);
const items = computed(() => snapshot.value?.items ?? []);
/** 事件登记了续期 / 补跑动作，就属于「系统可自动处理」。 */
function autoActionable(event: HealthEvent): boolean {
  return event.actions.some(
    (action) => action.kind === "credential_refresh" || action.kind === "activity_rerun",
  );
}
const autoCount = computed(() => items.value.filter(autoActionable).length);

/** 账号筛选只列当前事件流里出现过的号；名字优先取证据里的 accountName。 */
const accountOptions = computed(() => {
  const names = new Map<string, string>();
  for (const item of items.value) {
    if (!item.accountId) continue;
    const raw = item.evidence.accountName;
    names.set(item.accountId, typeof raw === "string" && raw ? raw : item.title);
  }
  return [...names.entries()]
    .map(([id, label]) => ({ id, label }))
    .sort((a, b) => a.label.localeCompare(b.label));
});

const filtering = computed(
  () => kindFilter.value !== "all" || Boolean(accountFilter.value) || Boolean(groupFilter.value),
);

function clearFilters(): void {
  kindFilter.value = "all";
  accountFilter.value = "";
  groupFilter.value = "";
}

const visible = computed(() =>
  items.value.filter(
    (item) =>
      (kindFilter.value === "all" || item.kind === kindFilter.value) &&
      (!groupFilter.value || GROUP_KINDS[groupFilter.value]?.includes(item.kind)) &&
      (!accountFilter.value || item.accountId === accountFilter.value) &&
      (!autoOnly.value || autoActionable(item)),
  ),
);
const selectable = computed(() => visible.value.filter((item) => item.state !== "resolved"));
const allSelected = computed(
  () =>
    selectable.value.length > 0 &&
    selectable.value.every((item) => selected.value.has(item.id)),
);
const anySelected = computed(() => selected.value.size > 0);

function kindLabel(kind: string): string {
  const key = KIND_LABEL[kind];
  return key ? t(key) : kind;
}

function stateLabel(state: HealthEventState): string {
  return t(`health.state.${state}` as MessageKey);
}

function severityLabel(severity: HealthSeverity): string {
  return t(`health.severity.${severity}` as MessageKey);
}

async function load(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    const data = await api.healthEvents(filter.value);
    snapshot.value = data;
    healthUnhandled.value = data.summary.unhandled;
    selected.value = new Set();
  } catch (exc) {
    error.value = (exc as Error).message;
  } finally {
    loading.value = false;
  }
}

function toggleSelect(event: HealthEvent): void {
  const next = new Set(selected.value);
  if (next.has(event.id)) next.delete(event.id);
  else next.add(event.id);
  selected.value = next;
}

function toggleSelectAll(): void {
  if (allSelected.value) {
    selected.value = new Set();
    return;
  }
  selected.value = new Set(selectable.value.map((item) => item.id));
}

async function runAction(event: HealthEvent, action: HealthAction): Promise<void> {
  if (action.kind === "navigate") {
    if (action.to) void router.push(action.to);
    return;
  }
  busy.value = event.id;
  flash.value = null;
  try {
    const result = await api.actHealthEvent(event.id, action.id);
    flash.value = { text: t("health.done", { message: result.message }), tone: "ok" };
    await load();
    if (auditOpen.value === event.id) await loadAudit(event.id);
  } catch (exc) {
    flash.value = { text: t("health.actionError", { error: (exc as Error).message }), tone: "bad" };
  } finally {
    busy.value = null;
  }
}

async function ack(event: HealthEvent): Promise<void> {
  busy.value = event.id;
  try {
    await api.ackHealthEvent(event.id);
    await load();
  } catch (exc) {
    flash.value = { text: t("health.actionError", { error: (exc as Error).message }), tone: "bad" };
  } finally {
    busy.value = null;
  }
}

async function snooze(event: HealthEvent, hours: number): Promise<void> {
  busy.value = event.id;
  try {
    await api.snoozeHealthEvent(event.id, hours);
    await load();
  } catch (exc) {
    flash.value = { text: t("health.actionError", { error: (exc as Error).message }), tone: "bad" };
  } finally {
    busy.value = null;
  }
}

async function bulk(op: "ack" | "snooze", hours = 24): Promise<void> {
  const ids = [...selected.value];
  if (!ids.length) return;
  busy.value = "bulk";
  try {
    const result = await api.bulkHealthEvents(ids, op, hours);
    flash.value = {
      text: t("health.bulkDone", { done: result.done, skipped: result.skipped }),
      tone: "ok",
    };
    await load();
  } catch (exc) {
    flash.value = { text: t("health.actionError", { error: (exc as Error).message }), tone: "bad" };
  } finally {
    busy.value = null;
  }
}

async function toggleAudit(event: HealthEvent): Promise<void> {
  if (auditOpen.value === event.id) {
    auditOpen.value = null;
    return;
  }
  auditOpen.value = event.id;
  if (!audits.value[event.id]) await loadAudit(event.id);
}

async function loadAudit(eventId: string): Promise<void> {
  try {
    const data = await api.healthEventActions(eventId);
    audits.value = { ...audits.value, [eventId]: data.items };
  } catch {
    audits.value = { ...audits.value, [eventId]: [] };
  }
}

/** 只展示认识的依据字段；未知字段不硬编，避免后端加字段时前端出现空白行。 */
const EVIDENCE_FIELDS: Array<{ key: string; labelKey: MessageKey }> = [
  { key: "accountName", labelKey: "health.evidence.account" },
  { key: "expiresAt", labelKey: "health.evidence.expiresAt" },
  { key: "failures", labelKey: "health.evidence.failures" },
  { key: "fingerprint", labelKey: "health.evidence.fingerprint" },
  { key: "clientVersion", labelKey: "health.evidence.version" },
  { key: "taskKey", labelKey: "health.evidence.task" },
  { key: "title", labelKey: "health.evidence.activity" },
  { key: "healthyAccounts", labelKey: "health.evidence.healthy" },
  { key: "creditsDays", labelKey: "health.evidence.creditsDays" },
];

function evidenceRows(event: HealthEvent): Array<{ label: string; value: string }> {
  const rows: Array<{ label: string; value: string }> = [];
  for (const field of EVIDENCE_FIELDS) {
    const raw = event.evidence[field.key];
    if (raw === null || raw === undefined || raw === "") continue;
    const value = field.key === "expiresAt" ? formatTime(String(raw)) : String(raw);
    rows.push({ label: t(field.labelKey), value });
  }
  return rows;
}

function isAuto(log: HealthActionLog): boolean {
  return log.action.startsWith("auto:");
}

onMounted(() => {
  void load();
  void loadBaseline();
});
</script>

<template>
  <div class="space-y-5">
    <PageHeader :title="t('health.title')" :subtitle="t('health.subtitle')">
      <template #actions>
        <button
          type="button"
          class="inline-flex items-center gap-2 rounded-control border border-line bg-surface px-3.5 py-2 text-body-sm font-medium text-ink-soft transition-colors hover:border-line-strong hover:text-ink"
          :disabled="loading"
          @click="load"
        >
          <RefreshCw :size="15" :stroke-width="1.9" :class="loading ? 'animate-spin' : ''" />
          {{ t("health.refresh") }}
        </button>
      </template>
    </PageHeader>

    <p
      v-if="flash"
      class="flex items-center gap-2 rounded-control border px-3.5 py-2.5 text-body-sm"
      :class="
        flash.tone === 'ok'
          ? 'border-brand/20 bg-brand-soft text-brand-dark'
          : 'border-danger/20 bg-danger-soft text-danger'
      "
    >
      <CheckCircle2 v-if="flash.tone === 'ok'" :size="15" :stroke-width="2" />
      <AlertTriangle v-else :size="15" :stroke-width="2" />
      {{ flash.text }}
    </p>

    <!-- 四格摘要：数字只来自后端同一份聚合，不在前端再算一遍。 -->
    <div v-if="summary" class="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <div class="rounded-card border border-line bg-surface px-4 py-3 shadow-card">
        <div class="flex items-center gap-1.5 text-caption text-muted">
          <Clock3 :size="13" :stroke-width="1.9" />
          {{ t("health.summary.unhandled") }}
        </div>
        <div class="mt-1.5 text-title font-semibold tabular-nums text-ink">
          {{ summary.unhandled }}
        </div>
      </div>
      <div class="rounded-card border border-line bg-surface px-4 py-3 shadow-card">
        <div class="flex items-center gap-1.5 text-caption text-muted">
          <AlertTriangle :size="13" :stroke-width="1.9" class="text-danger" />
          {{ t("health.summary.high") }}
        </div>
        <div class="mt-1.5 text-title font-semibold tabular-nums text-danger">
          {{ summary.high }}
        </div>
      </div>
      <div class="rounded-card border border-line bg-surface px-4 py-3 shadow-card">
        <div class="flex items-center gap-1.5 text-caption text-muted">
          <ShieldCheck :size="13" :stroke-width="1.9" class="text-warn" />
          {{ t("health.summary.warn") }}
        </div>
        <div class="mt-1.5 text-title font-semibold tabular-nums text-warn">
          {{ summary.warn }}
        </div>
      </div>
      <div class="rounded-card border border-line bg-surface px-4 py-3 shadow-card">
        <div class="flex items-center gap-1.5 text-caption text-muted">
          <CheckCircle2 :size="13" :stroke-width="1.9" class="text-brand" />
          {{ t("health.summary.resolved") }}
        </div>
        <div class="mt-1.5 text-title font-semibold tabular-nums text-ink">
          {{ summary.resolved }}
        </div>
      </div>
    </div>

    <!-- 系统基线：号池供给与上游结构各占一半；两块都没数据时整条不渲染 -->
    <div
      v-if="poolHealth || compat"
      class="rounded-card border border-line bg-surface shadow-card"
    >
      <div class="flex flex-wrap items-center gap-2 border-b border-line-soft px-4 py-2.5">
        <ShieldCheck :size="15" :stroke-width="1.9" class="text-brand" />
        <span class="text-body-sm font-medium text-ink">{{ t("health.baseline.title") }}</span>
      </div>
      <div class="grid divide-y divide-line-soft lg:grid-cols-2 lg:divide-x lg:divide-y-0">
        <div v-if="poolHealth" class="px-4 py-3">
          <div class="flex items-center gap-2">
            <Coins :size="13" :stroke-width="1.9" class="text-muted" />
            <span class="text-body-sm font-medium text-ink">{{ t("health.baseline.supply") }}</span>
            <StatusPill v-if="poolHealth.alerts.length" tone="warn" dot>
              {{ t("overview.supply.alert") }}
            </StatusPill>
          </div>
          <div class="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-body-sm text-ink-soft">
            <span>{{ t("overview.supply.healthy", { count: poolHealth.healthyAccounts, total: poolHealth.accounts }) }}</span>
            <span>{{ t("overview.supply.expiring", { count: poolHealth.expiring24h }) }}</span>
            <span>{{ t("overview.supply.cooling", { count: poolHealth.cooling }) }}</span>
            <span>{{ t("overview.supply.credits", { days: supplyDaysText(poolHealth.creditsDays) }) }}</span>
            <span>{{ t("overview.supply.success", { rate: supplyRateText(poolHealth.successRate24h) }) }}</span>
          </div>
          <p v-if="poolHealth.alerts.length" class="mt-1 text-caption leading-relaxed text-warn">
            {{ poolHealth.alerts[0].message }}
          </p>
        </div>

        <div v-if="compat" class="px-4 py-3">
          <div class="flex items-center gap-2">
            <Network :size="13" :stroke-width="1.9" class="text-muted" />
            <span class="text-body-sm font-medium text-ink">{{ t("health.baseline.compat") }}</span>
            <button
              type="button"
              class="ml-auto inline-flex items-center gap-1 rounded-control border border-line px-2 py-1 text-caption text-ink-soft transition-colors hover:border-line-strong hover:text-ink disabled:opacity-60"
              :disabled="compatBusy"
              @click="probeUpstream"
            >
              <Loader2 v-if="compatBusy" :size="11" class="animate-spin" />
              {{ compatBusy ? t("overview.compat.probing") : t("overview.compat.probe") }}
            </button>
          </div>
          <ul class="mt-1.5 flex flex-wrap gap-x-4 gap-y-1.5">
            <li
              v-for="item in compat.items"
              :key="item.scope"
              class="flex items-center gap-1.5 text-body-sm"
            >
              <span class="font-medium text-ink">
                {{ item.platform === "trae" ? "Trae" : "WorkBuddy" }}
              </span>
              <StatusPill :tone="COMPAT_TONE[item.state]">
                {{ t(COMPAT_LABEL[item.state]) }}
              </StatusPill>
              <span class="text-caption text-muted">
                {{ item.current?.clientVersion || t("overview.compat.noVersion") }}
              </span>
            </li>
          </ul>
          <p v-if="compat.changed.length" class="mt-1 text-caption text-warn">
            {{ t("overview.compat.changedHint", { scopes: compat.changed.join(" / ") }) }}
          </p>
        </div>
      </div>
    </div>

    <!-- 筛选条 -->
    <div class="flex flex-wrap items-center gap-2">
      <div class="inline-flex rounded-control border border-line bg-surface p-0.5">
        <button
          v-for="item in FILTERS"
          :key="item.key"
          type="button"
          class="rounded-[calc(var(--radius-control)-2px)] px-3 py-1.5 text-body-sm transition-colors"
          :class="
            filter === item.key
              ? 'bg-brand-soft font-medium text-brand-dark'
              : 'text-muted hover:text-ink'
          "
          @click="filter = item.key; load()"
        >
          {{ t(item.labelKey) }}
        </button>
      </div>

      <button
        v-if="autoCount"
        type="button"
        class="rounded-control border px-3 py-2 text-body-sm transition-colors"
        :class="
          autoOnly
            ? 'border-brand/30 bg-brand-soft font-medium text-brand-dark'
            : 'border-line bg-surface text-ink-soft hover:border-line-strong hover:text-ink'
        "
        @click="autoOnly = !autoOnly"
      >
        {{ t("health.filter.auto", { count: autoCount }) }}
      </button>

      <div class="relative">
        <select
          v-model="kindFilter"
          class="appearance-none rounded-control border border-line bg-surface py-2 pl-3 pr-8 text-body-sm text-ink-soft outline-none transition-colors hover:border-line-strong focus:border-brand"
          :aria-label="t('health.filter.kind')"
          @change="groupFilter = ''"
        >
          <option value="all">{{ t("health.filter.allKinds") }}</option>
          <option v-for="kind in KIND_OPTIONS" :key="kind" :value="kind">
            {{ kindLabel(kind) }}
          </option>
        </select>
        <ChevronDown
          :size="14"
          :stroke-width="1.9"
          class="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-muted"
        />
      </div>

      <div v-if="accountOptions.length" class="relative">
        <select
          v-model="accountFilter"
          class="appearance-none rounded-control border border-line bg-surface py-2 pl-3 pr-8 text-body-sm text-ink-soft outline-none transition-colors hover:border-line-strong focus:border-brand"
          :aria-label="t('health.filter.account')"
        >
          <option value="">{{ t("health.filter.allAccounts") }}</option>
          <option v-for="option in accountOptions" :key="option.id" :value="option.id">
            {{ option.label }}
          </option>
        </select>
        <ChevronDown
          :size="14"
          :stroke-width="1.9"
          class="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-muted"
        />
      </div>

      <span
        v-if="groupFilter"
        class="inline-flex items-center gap-1 rounded-control border border-brand/30 bg-brand-soft px-2.5 py-2 text-body-sm text-brand-dark"
      >
        {{ t(GROUP_LABEL[groupFilter] ?? "health.filter.kind") }}
        <button
          type="button"
          class="rounded p-0.5 hover:bg-surface/70"
          :aria-label="t('health.filter.clear')"
          @click="groupFilter = ''"
        >
          <X :size="11" :stroke-width="2.4" />
        </button>
      </span>

      <button
        v-if="filtering"
        type="button"
        class="text-caption text-muted transition-colors hover:text-ink"
        @click="clearFilters"
      >
        {{ t("health.filter.clear") }}
      </button>

      <button
        v-if="selectable.length"
        type="button"
        class="rounded-control border border-line bg-surface px-3 py-2 text-body-sm text-ink-soft transition-colors hover:border-line-strong hover:text-ink"
        @click="toggleSelectAll"
      >
        {{ allSelected ? t("health.action.clear") : t("health.action.selectAll") }}
      </button>

      <div v-if="anySelected" class="ml-auto flex flex-wrap items-center gap-2">
        <span class="text-body-sm text-muted">
          {{ t("health.action.selected", { count: selected.size }) }}
        </span>
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-control border border-brand/30 bg-brand-soft px-3 py-2 text-body-sm font-medium text-brand-dark disabled:opacity-60"
          :disabled="busy === 'bulk'"
          @click="bulk('ack')"
        >
          <Check :size="14" :stroke-width="2" />
          {{ t("health.action.bulkAck") }}
        </button>
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-control border border-line bg-surface px-3 py-2 text-body-sm text-ink-soft disabled:opacity-60"
          :disabled="busy === 'bulk'"
          @click="bulk('snooze', 24)"
        >
          <Clock3 :size="14" :stroke-width="1.9" />
          {{ t("health.action.bulkSnooze") }}
        </button>
      </div>
    </div>

    <p v-if="error" class="rounded-control border border-danger/30 bg-danger-soft px-3.5 py-2.5 text-body-sm text-danger">
      {{ t("health.loadError", { error }) }}
    </p>

    <div v-if="loading" class="space-y-3">
      <Skeleton v-for="n in 3" :key="n" class="h-28 rounded-card" />
    </div>

    <div v-else-if="!visible.length" class="rounded-card border border-dashed border-line bg-surface px-6 py-14 text-center">
      <CheckCircle2 :size="28" :stroke-width="1.5" class="mx-auto text-brand" />
      <p class="mt-3 text-body font-medium text-ink">
        {{ filter === "resolved" ? t("health.empty.resolved.title") : t("health.empty.active.title") }}
      </p>
      <p class="mx-auto mt-1 max-w-[46ch] text-body-sm leading-relaxed text-muted">
        {{ filter === "resolved" ? t("health.empty.resolved.desc") : t("health.empty.active.desc") }}
      </p>
    </div>

    <div v-else class="space-y-3">
      <article
        v-for="event in visible"
        :key="event.id"
        class="rounded-card border border-line bg-surface shadow-card transition-colors hover:border-line-strong"
      >
        <div class="flex">
          <span
            class="w-1 shrink-0 rounded-l-card"
            :class="
              event.severity === 'high'
                ? 'bg-danger'
                : event.severity === 'warn'
                  ? 'bg-warn'
                  : 'bg-info'
            "
          />
          <div class="min-w-0 flex-1 p-4">
            <div class="flex flex-wrap items-start gap-2">
              <label
                v-if="event.state !== 'resolved'"
                class="mt-0.5 inline-flex cursor-pointer items-center"
                :title="t('health.action.bulkAck')"
              >
                <input
                  type="checkbox"
                  class="size-3.5 accent-brand"
                  :checked="selected.has(event.id)"
                  @change="toggleSelect(event)"
                />
              </label>
              <div class="min-w-0 flex-1">
                <div class="flex flex-wrap items-center gap-2">
                  <StatusPill :tone="SEVERITY_TONE[event.severity]" dot>
                    {{ severityLabel(event.severity) }}
                  </StatusPill>
                  <span class="text-caption text-muted">{{ kindLabel(event.kind) }}</span>
                  <StatusPill :tone="STATE_TONE[event.state]">
                    {{ stateLabel(event.state) }}
                  </StatusPill>
                  <span class="text-caption text-muted">
                    {{ formatTime(event.lastSeenAt) }}
                  </span>
                </div>
                <h3 class="mt-1.5 text-body font-semibold text-ink">{{ event.title }}</h3>
                <p class="mt-1 text-body-sm leading-relaxed text-ink-soft">{{ event.detail }}</p>
              </div>
            </div>

            <dl
              v-if="evidenceRows(event).length"
              class="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 rounded-control bg-line-soft/50 px-3 py-2 sm:grid-cols-3"
            >
              <div v-for="row in evidenceRows(event)" :key="row.label" class="min-w-0">
                <dt class="text-micro text-muted">{{ row.label }}</dt>
                <dd class="truncate text-body-sm text-ink-soft" :title="row.value">{{ row.value }}</dd>
              </div>
            </dl>

            <div class="mt-3 flex flex-wrap items-center gap-2">
              <button
                v-for="action in event.actions"
                :key="action.id"
                type="button"
                class="inline-flex items-center gap-1.5 rounded-control border px-3 py-1.5 text-body-sm transition-colors disabled:opacity-60"
                :class="
                  action.kind === 'navigate'
                    ? 'border-line bg-surface text-ink-soft hover:border-line-strong hover:text-ink'
                    : 'border-brand/30 bg-brand-soft font-medium text-brand-dark hover:border-brand/50'
                "
                :disabled="busy === event.id"
                @click="runAction(event, action)"
              >
                <Loader2
                  v-if="busy === event.id && action.kind !== 'navigate'"
                  :size="13"
                  :stroke-width="2"
                  class="animate-spin"
                />
                {{ action.label }}
              </button>

              <button
                v-if="event.state === 'open'"
                type="button"
                class="inline-flex items-center gap-1.5 rounded-control border border-line px-3 py-1.5 text-body-sm text-muted transition-colors hover:text-ink"
                :disabled="busy === event.id"
                @click="ack(event)"
              >
                <Check :size="13" :stroke-width="2" />
                {{ t("health.action.ack") }}
              </button>

              <details v-if="event.state === 'open' || event.state === 'acked'" class="relative">
                <summary
                  class="inline-flex cursor-pointer list-none items-center gap-1.5 rounded-control border border-line px-3 py-1.5 text-body-sm text-muted transition-colors hover:text-ink"
                >
                  <Clock3 :size="13" :stroke-width="1.9" />
                  {{ t("health.action.snooze") }}
                  <ChevronDown :size="12" :stroke-width="2" />
                </summary>
                <div class="absolute left-0 z-30 mt-1 min-w-[9rem] rounded-control border border-line bg-surface p-1 shadow-card-hover">
                  <button
                    v-for="option in SNOOZE_OPTIONS"
                    :key="option.hours"
                    type="button"
                    class="block w-full rounded px-2.5 py-1.5 text-left text-body-sm text-ink-soft hover:bg-line-soft hover:text-ink"
                    @click="snooze(event, option.hours)"
                  >
                    {{ t(option.labelKey) }}
                  </button>
                </div>
              </details>

              <button
                type="button"
                class="ml-auto inline-flex items-center gap-1.5 rounded-control px-2 py-1.5 text-body-sm text-muted transition-colors hover:text-ink"
                @click="toggleAudit(event)"
              >
                <History :size="13" :stroke-width="1.9" />
                {{ t("health.action.detail") }}
              </button>
            </div>

            <div v-if="auditOpen === event.id" class="mt-3 border-t border-line-soft pt-3">
              <p v-if="!audits[event.id]?.length" class="text-body-sm text-muted">
                {{ t("health.audit.empty") }}
              </p>
              <ul v-else class="space-y-1.5">
                <li
                  v-for="(log, index) in audits[event.id]"
                  :key="`${log.at}-${index}`"
                  class="flex flex-wrap items-center gap-2 text-body-sm"
                >
                  <StatusPill :tone="log.ok ? 'brand' : 'danger'">
                    {{ log.ok ? "OK" : "ERR" }}
                  </StatusPill>
                  <span v-if="isAuto(log)" class="text-caption text-muted">
                    {{ t("health.audit.auto") }}
                  </span>
                  <span class="text-ink-soft">{{ log.message }}</span>
                  <span class="ml-auto text-caption text-muted">{{ formatTime(log.at) }}</span>
                </li>
              </ul>
            </div>
          </div>
        </div>
      </article>
    </div>
  </div>
</template>

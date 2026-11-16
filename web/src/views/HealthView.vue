<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  Clock3,
  History,
  Loader2,
  RefreshCw,
  ShieldCheck,
} from "lucide-vue-next";
import PageHeader from "../components/PageHeader.vue";
import Skeleton from "../components/Skeleton.vue";
import StatusPill from "../components/StatusPill.vue";
import {
  api,
  formatTime,
  type HealthAction,
  type HealthActionLog,
  type HealthEvent,
  type HealthEventState,
  type HealthSeverity,
  type HealthSnapshot,
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

const snapshot = ref<HealthSnapshot | null>(null);
const filter = ref<"active" | "resolved" | "all">("active");
const kindFilter = ref("all");
const loading = ref(true);
const error = ref("");
const busy = ref<string | null>(null);
const selected = ref<Set<string>>(new Set());
const auditOpen = ref<string | null>(null);
const audits = ref<Record<string, HealthActionLog[]>>({});
const flash = ref<{ text: string; tone: "ok" | "bad" } | null>(null);

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
const visible = computed(() =>
  items.value.filter((item) => kindFilter.value === "all" || item.kind === kindFilter.value),
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

onMounted(load);
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

      <div class="relative">
        <select
          v-model="kindFilter"
          class="appearance-none rounded-control border border-line bg-surface py-2 pl-3 pr-8 text-body-sm text-ink-soft outline-none transition-colors hover:border-line-strong focus:border-brand"
          :aria-label="t('health.filter.kind')"
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

    <p v-if="error" class="rounded-control border border-danger/20 bg-danger-soft px-3.5 py-2.5 text-body-sm text-danger">
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

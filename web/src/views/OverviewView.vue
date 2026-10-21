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
import SectionCard from "../components/SectionCard.vue";
import StatCard from "../components/StatCard.vue";
import StatusPill from "../components/StatusPill.vue";
import { api, formatBytes, formatTime, type ActionableItem, type OverviewPulse } from "../api";
import { accounts, attentionCount, clientDetections, detection, snapshots } from "../stores/app";
import { t } from "../lib/i18n";

const router = useRouter();

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

/** 服务端聚合的待处理清单（凭据 / 签到 / 积分到期 / 客户端未运行）。 */
const actionables = ref<ActionableItem[]>([]);
const pulse = ref<OverviewPulse | null>(null);
const loadError = ref("");
const busy = ref<string | null>(null);

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

async function load(): Promise<void> {
  try {
    const next = await api.overviewActionables();
    actionables.value = next.items;
    pulse.value = next.pulse;
    loadError.value = "";
  } catch (cause) {
    // 读不到就说读不到，不能渲染成「暂无待处理」——那等于骗用户一切正常。
    loadError.value = cause instanceof Error ? cause.message : String(cause);
  }
}

function keyOf(item: ActionableItem, index: number): string {
  return `${item.kind}-${item.accountId ?? items.value.length + index}`;
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

function actionLabel(item: ActionableItem): string {
  if (item.action.type === "run") return t("overview.action.run");
  if (item.kind === "onboarding") return t("overview.action.accounts");
  return t("overview.action.handle");
}

function busyFor(item: ActionableItem): boolean {
  return busy.value !== null && busy.value === (item.accountId ?? item.kind);
}

/** 按 action.type 分发：跳页交给 router，执行类调用对应接口后静默刷新清单。 */
async function trigger(item: ActionableItem): Promise<void> {
  if (item.action.type === "navigate") {
    await router.push(item.action.to);
    return;
  }
  const token = item.accountId ?? item.kind;
  busy.value = token;
  try {
    if (item.action.endpoint === "/activities/run") {
      await api.runActivities(item.action.accountIds);
    }
    await load();
  } catch (cause) {
    loadError.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    busy.value = null;
  }
}

onMounted(() => void load());

const clientText = computed(() => {
  if (!detection.value) return t("common.unknown");
  if (!detection.value.installed) return t("overview.client.notInstalled");
  return detection.value.running ? t("overview.client.running") : t("overview.client.stopped");
});

const latestSnapshot = computed(() => snapshots.value[0] ?? null);
</script>

<template>
  <div>
    <div class="flex items-start justify-between gap-4">
      <div>
        <h1 class="text-[23.5px] font-semibold tracking-[-0.02em] text-ink">{{ t("nav.overview") }}</h1>
        <p class="mt-1.5 text-[13px] text-muted">{{ subtitle }}</p>
      </div>
      <RouterLink
        to="/accounts"
        class="inline-flex items-center gap-1.5 rounded-[9px] border border-line bg-surface px-3 py-2 text-[12.5px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink"
      >
        <Users :size="14" :stroke-width="1.9" /> {{ t("overview.manageAccounts") }}
      </RouterLink>
    </div>

    <div class="mt-5 grid gap-4 lg:grid-cols-[1.6fr_1fr]">
      <SectionCard :title="t('overview.actionables.title')" :subtitle="t('overview.actionables.count', { count: items.length })" flush>
        <p v-if="loadError" class="px-5 py-3.5 text-[12.5px] text-danger">
          {{ t("overview.actionables.error", { message: loadError }) }}
        </p>
        <div
          v-else-if="!items.length"
          class="flex items-center gap-2.5 px-5 py-8 text-[13px] text-ink-soft"
        >
          <CheckCircle2 :size="16" class="text-brand" />
          {{ t("overview.actionables.empty") }}
        </div>
        <ul v-else class="divide-y divide-line-soft">
          <li
            v-for="(item, index) in items"
            :key="keyOf(item, index)"
            class="flex items-start gap-3 px-5 py-3.5"
          >
            <AlertTriangle
              :size="15"
              class="mt-0.5 shrink-0"
              :class="item.severity === 'info' ? 'text-muted' : 'text-warn'"
            />
            <div class="min-w-0 flex-1">
              <div class="text-[13px] font-medium text-ink">{{ item.title }}</div>
              <div class="mt-0.5 text-[12px] leading-relaxed text-muted">{{ item.detail }}</div>
            </div>
            <StatusPill :tone="severityTone(item.severity)" class="mt-0.5 shrink-0">
              {{ severityLabel(item.severity) }}
            </StatusPill>
            <button
              type="button"
              class="inline-flex shrink-0 items-center gap-1 rounded-[8px] border border-line px-2.5 py-1.5 text-[12px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink disabled:opacity-50"
              :disabled="busyFor(item)"
              @click="trigger(item)"
            >
              <Loader2 v-if="busyFor(item)" :size="11" class="animate-spin" />
              <template v-else>{{ actionLabel(item) }} <ArrowRight :size="11" /></template>
            </button>
          </li>
        </ul>
      </SectionCard>

      <SectionCard :title="t('overview.client.title')" :subtitle="t('overview.client.platformCount', { count: clientDetections.length })">
        <div v-if="!clientDetections.length" class="space-y-2.5 text-[12.5px]">
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
            <div class="mt-1 break-all rounded-[8px] bg-line-soft px-2 py-1.5 font-mono text-[11px] text-ink-soft">
              {{ detection.stateDir }}
            </div>
          </div>
        </div>

        <div v-else class="space-y-3 text-[12.5px]">
          <div
            v-for="client in clientDetections"
            :key="client.scope"
            class="rounded-[10px] border border-line-soft p-3"
          >
            <div class="flex items-center justify-between gap-2">
              <span class="font-medium text-ink">{{ client.label }}</span>
              <div class="flex items-center gap-1.5">
                <StatusPill :tone="client.running ? 'warn' : 'neutral'" dot>
                  {{ client.running ? t("overview.client.running") : t("overview.client.stopped") }}
                </StatusPill>
                <StatusPill :tone="client.loggedIn ? 'brand' : 'neutral'">
                  {{ client.loggedIn ? t("overview.client.loggedIn") : t("overview.client.loggedOut") }}
                </StatusPill>
              </div>
            </div>
            <div v-if="client.stateDir" class="mt-2">
              <div class="text-[11px] text-muted">{{ t("overview.client.stateDir") }}</div>
              <div class="mt-1 break-all rounded-[8px] bg-line-soft px-2 py-1.5 font-mono text-[10.5px] text-ink-soft">
                {{ client.stateDir }}
              </div>
            </div>
            <div v-if="client.message" class="mt-1.5 text-[11px] text-muted">{{ client.message }}</div>
          </div>
        </div>
      </SectionCard>
    </div>

    <SectionCard
      class="mt-4"
      :title="t('overview.pulse.title')"
      :subtitle="t('overview.pulse.subtitle')"
    >
      <div v-if="!pulse" class="text-[12.5px] text-muted">
        {{ t("overview.pulse.loading") }}
      </div>
      <div v-else class="grid gap-3 sm:grid-cols-3">
        <RouterLink
          to="/activity"
          class="rounded-[10px] border border-line-soft bg-canvas/60 px-3.5 py-3 transition-colors hover:border-brand/40"
        >
          <div class="flex items-center gap-1.5 text-[11.5px] text-muted">
            <Activity :size="13" /> {{ t("overview.pulse.activity") }}
          </div>
          <div class="mt-1 text-[19px] font-semibold tabular-nums leading-none text-ink">
            {{ pulse.activityActionable }}
          </div>
          <div class="mt-0.5 text-[11px] text-muted">
            {{
              t("overview.pulse.activityHint", {
                done: pulse.activityDone,
                total: pulse.activityTotal,
              })
            }}
          </div>
        </RouterLink>

        <RouterLink
          to="/usage"
          class="rounded-[10px] border border-line-soft bg-canvas/60 px-3.5 py-3 transition-colors hover:border-brand/40"
        >
          <div class="flex items-center gap-1.5 text-[11.5px] text-muted">
            <Coins :size="13" /> {{ t("overview.pulse.credits") }}
          </div>
          <div class="mt-1 text-[19px] font-semibold tabular-nums leading-none text-ink">{{ pulse.credits }}</div>
          <div class="mt-0.5 text-[11px]" :class="pulse.creditsExpiring ? 'text-warn' : 'text-muted'">
            {{ t("overview.pulse.creditsHint", { count: pulse.creditsExpiring }) }}
          </div>
        </RouterLink>

        <RouterLink
          to="/gateway"
          class="rounded-[10px] border border-line-soft bg-canvas/60 px-3.5 py-3 transition-colors hover:border-brand/40"
        >
          <div class="flex items-center gap-1.5 text-[11.5px] text-muted">
            <Network :size="13" /> {{ t("overview.pulse.gateway") }}
          </div>
          <div class="mt-1 text-[19px] font-semibold tabular-nums leading-none text-ink">
            {{ pulse.gateway.requests }}
          </div>
          <div class="mt-0.5 text-[11px] text-muted">
            {{
              t("overview.pulse.gatewayHint", {
                tokens: pulse.gateway.tokens,
                keys: pulse.gateway.activeKeys,
                errors: pulse.gateway.errors,
              })
            }}
          </div>
        </RouterLink>
      </div>
    </SectionCard>

    <div class="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <StatCard :label="t('overview.stat.accounts')" :value="String(accounts.length)" :icon="Users" :hint="t('overview.stat.accountsHint')" />
      <StatCard
        :label="t('overview.stat.attention')"
        :value="String(attentionCount)"
        :icon="AlertTriangle"
        :hint="attentionCount ? t('overview.stat.attentionHint') : t('overview.stat.allGood')"
      />
      <StatCard
        :label="t('overview.stat.snapshots')"
        :value="String(snapshots.length)"
        :icon="Database"
        :hint="
          latestSnapshot
            ? t('overview.stat.lastBackup', { time: formatTime(latestSnapshot.createdAt) })
            : t('overview.stat.noBackups')
        "
      />
      <StatCard :label="t('overview.client.title')" :value="clientText" :icon="MonitorSmartphone" :hint="t('overview.stat.clientHint')" />
    </div>

    <div class="mt-4">
      <SectionCard :title="t('overview.snapshots.title')" flush>
        <div v-if="!snapshots.length" class="px-5 py-8 text-center text-[13px] text-muted">
          {{ t("overview.snapshots.empty") }}
        </div>
        <ul v-else class="divide-y divide-line-soft">
          <li
            v-for="snapshot in snapshots.slice(0, 4)"
            :key="snapshot.id"
            class="flex items-center gap-4 px-5 py-3 text-[12.5px]"
          >
            <Repeat2 :size="14" class="shrink-0 text-muted" />
            <span class="w-24 shrink-0 text-ink-soft">
              {{
                snapshot.reason === "manual"
                  ? t("overview.snapshots.manual")
                  : snapshot.reason === "auto"
                    ? t("overview.snapshots.auto")
                    : t("overview.snapshots.beforeSwitch")
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
  </div>
</template>

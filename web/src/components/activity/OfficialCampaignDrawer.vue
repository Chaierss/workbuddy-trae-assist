<script setup lang="ts">
/**
 * 官方活动详情抽屉。
 *
 * 卡片回答「这个活动整体怎么样」，抽屉回答「具体是哪些账号、卡在哪一步」。
 * 抽屉里的「执行本活动」只提交当前活动的 key，不会顺带跑其它活动。
 */
import { computed, ref, watch } from "vue";
import {
  AlertTriangle,
  CalendarCheck,
  CheckCircle2,
  CircleDashed,
  Clock,
  ExternalLink,
  Flame,
  GraduationCap,
  Loader2,
  Moon,
  Plane,
  Sprout,
  Sun,
  X,
  type LucideIcon,
} from "lucide-vue-next";
import StatusPill from "../StatusPill.vue";
import CampaignProgress from "./CampaignProgress.vue";
import {
  CAMPAIGN_STATUS,
  CAMPAIGN_THEME,
  buildTrend,
  campaignPeriod,
  safeActivityUrl,
  type TrendPoint,
} from "../../lib/activity";
import { t, type MessageKey } from "../../lib/i18n";
import { api } from "../../api";
import type {
  AccountActivities,
  ActivityCampaign,
  ActivityRun,
  ActivityState,
  CampaignIcon,
} from "../../api";

const props = withDefaults(
  defineProps<{
    campaign: ActivityCampaign | null;
    accounts: AccountActivities[];
    runs?: ActivityRun[];
    running?: boolean;
  }>(),
  { runs: () => [], running: false },
);

const emit = defineEmits<{
  close: [];
  run: [accountIds: string[], keys: string[]];
}>();

type RowState = ActivityState | "undetected";

const STATE_TEXT: Record<RowState, MessageKey> = {
  done: "activity.stateDone",
  todo: "activity.stateTodo",
  in_progress: "activity.stateInProgress",
  not_started: "activity.stateNotStarted",
  unavailable: "activity.stateUnavailable",
  undetected: "activity.stateUndetected",
};

const STATE_ICON = {
  done: CheckCircle2,
  todo: CircleDashed,
  in_progress: Clock,
  not_started: CircleDashed,
  unavailable: AlertTriangle,
  undetected: CircleDashed,
};

const STATE_ORDER: Record<RowState, number> = {
  todo: 0,
  in_progress: 1,
  not_started: 2,
  unavailable: 3,
  undetected: 4,
  done: 5,
};

const CAMPAIGN_ICONS: Record<CampaignIcon, LucideIcon> = {
  "calendar-check": CalendarCheck,
  sprout: Sprout,
  flame: Flame,
  plane: Plane,
  moon: Moon,
  "graduation-cap": GraduationCap,
  sun: Sun,
};

const campaignIcon = computed(() =>
  CAMPAIGN_ICONS[props.campaign?.icon ?? "calendar-check"] ?? CalendarCheck,
);

const status = computed(() =>
  props.campaign ? CAMPAIGN_STATUS[props.campaign.status] : CAMPAIGN_STATUS.no_accounts,
);
const theme = computed(() =>
  props.campaign ? CAMPAIGN_THEME[props.campaign.theme] : CAMPAIGN_THEME.green,
);
const officialUrl = computed(() => safeActivityUrl(props.campaign?.officialUrl));
const period = computed(() =>
  props.campaign
    ? campaignPeriod(props.campaign.periodStart, props.campaign.periodEnd)
    : "",
);

const TREND_DAYS = 14;
const trend = ref<TrendPoint[]>([]);
const trendLoading = ref(false);
const trendHasData = computed(() => trend.value.some((point) => point.total > 0));
const trendMax = computed(() =>
  Math.max(1, ...trend.value.map((point) => point.total)),
);
const trendSummary = computed(() =>
  trend.value.reduce(
    (acc, point) => ({
      ok: acc.ok + point.ok,
      skipped: acc.skipped + point.skipped,
      failed: acc.failed + point.failed,
    }),
    { ok: 0, skipped: 0, failed: 0 },
  ),
);

async function loadTrend(key: string): Promise<void> {
  trendLoading.value = true;
  try {
    const history = await api.activityHistory(TREND_DAYS, key);
    trend.value = buildTrend(history.series[0], TREND_DAYS, history.until);
  } catch {
    // 趋势是辅助信息：读不到就只显示空态，不打断抽屉主流程。
    trend.value = buildTrend(undefined, TREND_DAYS, new Date().toISOString().slice(0, 10));
  } finally {
    trendLoading.value = false;
  }
}

watch(
  () => props.campaign?.key,
  (key) => {
    if (key) void loadTrend(key);
    else trend.value = [];
  },
  { immediate: true },
);

/** 账号 × 当前活动 → 一行；不可检测与缺项分别标明原因。 */
const rows = computed(() => {
  const campaign = props.campaign;
  if (!campaign) return [];
  return props.accounts
    .filter((account) => account.platform === campaign.platform)
    .map((account) => {
      const item = account.activities.find((row) => row.key === campaign.key);
      const state: RowState = !account.detectable
        ? "unavailable"
        : item
          ? item.state
          : "undetected";
      return {
        id: account.accountId,
        name: account.displayName,
        state,
        item,
        reason: account.error ?? "",
        actionable: Boolean(item?.automatable && item.state === "todo"),
      };
    })
    .sort(
      (a, b) =>
        STATE_ORDER[a.state] - STATE_ORDER[b.state] || a.name.localeCompare(b.name),
    );
});

const actionableRows = computed(() => rows.value.filter((row) => row.actionable));
const campaignRuns = computed(() =>
  props.runs.filter((run) => run.key === props.campaign?.key).slice(0, 6),
);

function runCampaign(): void {
  if (!props.campaign || !actionableRows.value.length || props.running) return;
  emit(
    "run",
    actionableRows.value.map((row) => row.id),
    [props.campaign.key],
  );
}
</script>

<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition-opacity duration-200"
      enter-from-class="opacity-0"
      leave-active-class="transition-opacity duration-150"
      leave-to-class="opacity-0"
    >
      <div
        v-if="campaign"
        class="fixed inset-0 z-50 bg-ink/25 backdrop-blur-[1px]"
        @click="emit('close')"
      />
    </Transition>
    <Transition
      enter-active-class="transition-transform duration-200"
      enter-from-class="translate-x-full"
      leave-active-class="transition-transform duration-150"
      leave-to-class="translate-x-full"
    >
      <aside
        v-if="campaign"
        class="fixed inset-y-0 right-0 z-[51] flex w-full max-w-[520px] flex-col border-l border-line bg-surface shadow-2xl"
        role="dialog"
        aria-modal="true"
      >
        <header class="flex items-start gap-3 border-b border-line px-5 py-4">
          <span
            class="mt-0.5 grid size-10 shrink-0 place-items-center rounded-control"
            :class="theme.icon"
          >
            <component :is="campaignIcon" :size="18" />
          </span>
          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-center gap-2">
              <h2 class="text-heading font-semibold text-ink">{{ campaign.title }}</h2>
              <StatusPill :tone="status.tone">{{ t(status.labelKey) }}</StatusPill>
              <StatusPill v-if="campaign.featured" tone="brand">
                {{ t("activity.officialFeatured") }}
              </StatusPill>
            </div>
            <p class="mt-1 text-body-sm text-muted">{{ campaign.subtitle }}</p>
          </div>
          <button
            type="button"
            class="grid size-7 shrink-0 place-items-center rounded-control text-muted transition-colors hover:bg-line-soft hover:text-ink"
            :title="t('activity.officialClose')"
            @click="emit('close')"
          >
            <X :size="16" />
          </button>
        </header>

        <div class="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <CampaignProgress :campaign="campaign" />

          <section class="mt-4">
            <div class="flex items-baseline justify-between gap-2">
              <h3 class="text-body font-medium text-ink">
                {{ t("activity.officialTrendTitle", { days: TREND_DAYS }) }}
              </h3>
              <span class="text-caption text-muted">
                {{ t("activity.officialTrendLegend", trendSummary) }}
              </span>
            </div>
            <div
              v-if="trendLoading"
              class="mt-2 h-16 animate-pulse rounded-control bg-line-soft"
              aria-hidden="true"
            />
            <template v-else>
              <div
                v-if="trendHasData"
                class="mt-2 flex h-16 items-end gap-1"
                role="img"
                :aria-label="t('activity.officialTrendTitle', { days: TREND_DAYS })"
              >
                <div
                  v-for="point in trend"
                  :key="point.day"
                  class="flex min-w-0 flex-1 flex-col justify-end gap-0.5"
                  :title="
                    t('activity.officialTrendDay', {
                      day: point.day,
                      ok: point.ok,
                      skipped: point.skipped,
                      failed: point.failed,
                    })
                  "
                >
                  <div
                    v-if="point.ok"
                    class="rounded-sm bg-brand"
                    :style="{ height: `${Math.max(2, (point.ok / trendMax) * 48)}px` }"
                  />
                  <div
                    v-if="point.skipped"
                    class="rounded-sm bg-line-strong"
                    :style="{ height: `${Math.max(2, (point.skipped / trendMax) * 48)}px` }"
                  />
                  <div
                    v-if="point.failed"
                    class="rounded-sm bg-danger"
                    :style="{ height: `${Math.max(2, (point.failed / trendMax) * 48)}px` }"
                  />
                  <div v-if="!point.total" class="h-[2px] rounded-sm bg-line-soft" />
                </div>
              </div>
              <p v-else class="mt-2 rounded-control bg-line-soft px-3 py-2 text-body-sm text-muted">
                {{ t("activity.officialTrendEmpty", { days: TREND_DAYS }) }}
              </p>
              <div v-if="trendHasData" class="mt-1 flex justify-between text-caption text-muted">
                <span>{{ trend[0]?.label }}</span>
                <span>{{ trend[trend.length - 1]?.label }}</span>
              </div>
            </template>
          </section>

          <p class="mt-4 text-body-sm leading-relaxed text-ink-soft">
            {{ campaign.description }}
          </p>

          <div class="mt-4 flex flex-wrap items-center gap-2">
            <span
              v-for="tag in campaign.tags"
              :key="tag"
              class="rounded-full border border-line px-2 py-0.5 text-caption text-muted"
            >
              {{ tag }}
            </span>
            <span class="text-caption text-muted">{{ campaign.periodNote }}</span>
            <span v-if="period" class="text-caption text-muted">{{ period }}</span>
          </div>

          <button
            type="button"
            class="mt-4 inline-flex w-full items-center justify-center gap-1.5 rounded-control bg-brand px-3.5 py-2 text-body-sm font-medium text-white transition-colors hover:bg-brand-dark disabled:opacity-40"
            :disabled="!campaign.actionable || running"
            @click="runCampaign"
          >
            <Loader2 v-if="running" :size="14" class="animate-spin" />
            <template v-else>
              {{ t("activity.officialRunCount", { count: campaign.actionable }) }}
            </template>
          </button>
          <p class="mt-1 text-center text-caption text-muted">
            {{ t("activity.officialRunKeysHint") }}
          </p>

          <section class="mt-5">
            <h3 class="text-body font-medium text-ink">{{ t("activity.officialRules") }}</h3>
            <ul class="mt-1.5 space-y-1 text-body-sm text-muted">
              <li v-for="rule in campaign.rules" :key="rule" class="flex gap-1.5">
                <span class="mt-[7px] size-1 shrink-0 rounded-full bg-line-strong" />
                <span>{{ rule }}</span>
              </li>
            </ul>
          </section>

          <section class="mt-4">
            <h3 class="text-body font-medium text-ink">{{ t("activity.officialRewards") }}</h3>
            <ul class="mt-1.5 space-y-1 text-body-sm text-muted">
              <li v-for="reward in campaign.rewards" :key="reward" class="flex gap-1.5">
                <span class="mt-[7px] size-1 shrink-0 rounded-full bg-brand/60" />
                <span>{{ reward }}</span>
              </li>
            </ul>
            <p class="mt-1 text-caption text-muted">{{ campaign.rewardHint }}</p>
          </section>

          <section class="mt-5">
            <div class="flex items-baseline justify-between gap-2">
              <h3 class="text-body font-medium text-ink">{{ t("activity.officialAccountsTitle") }}</h3>
              <span class="text-caption text-muted">
                {{ t("activity.officialAccountsCount", { count: rows.length }) }}
              </span>
            </div>
            <div v-if="!rows.length" class="mt-2 rounded-control bg-line-soft px-3 py-2 text-body-sm text-muted">
              {{ t("activity.officialAccountsEmpty") }}
            </div>
            <ul v-else class="mt-1.5 divide-y divide-line-soft">
              <li
                v-for="row in rows"
                :key="row.id"
                class="flex items-start gap-2 py-2 text-body-sm first:pt-0 last:pb-0"
              >
                <component
                  :is="STATE_ICON[row.state]"
                  :size="13"
                  class="mt-0.5 shrink-0"
                  :class="
                    row.state === 'done'
                      ? 'text-brand'
                      : row.state === 'unavailable'
                        ? 'text-danger'
                        : 'text-muted'
                  "
                />
                <div class="min-w-0 flex-1">
                  <div class="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                    <span class="truncate text-ink-soft">{{ row.name }}</span>
                    <span class="text-caption text-muted">{{ t(STATE_TEXT[row.state]) }}</span>
                    <span v-if="row.item?.progressText" class="text-caption text-muted">
                      {{ row.item.progressText }}
                    </span>
                  </div>
                  <div v-if="row.reason" class="text-caption text-danger">{{ row.reason }}</div>
                  <div v-else-if="row.item?.note" class="text-caption text-muted">
                    {{ row.item.note }}
                  </div>
                  <div v-if="row.item?.rewardText" class="text-caption text-brand-dark">
                    {{ row.item.rewardText }}
                  </div>
                  <div v-else-if="row.item?.manualReason" class="text-caption text-muted">
                    {{ row.item.manualReason }}
                  </div>
                </div>
                <span v-if="row.actionable" class="shrink-0 text-caption text-warn">
                  {{ t("activity.stateTodo") }}
                </span>
              </li>
            </ul>
          </section>

          <section v-if="campaignRuns.length" class="mt-5">
            <h3 class="text-body font-medium text-ink">{{ t("activity.officialRecentRuns") }}</h3>
            <ul class="mt-1.5 space-y-1">
              <li
                v-for="run in campaignRuns"
                :key="`${run.accountId}-${run.at}`"
                class="flex items-center gap-2 text-caption text-muted"
              >
                <span
                  class="size-1.5 shrink-0 rounded-full"
                  :class="
                    run.status === 'ok'
                      ? 'bg-brand'
                      : run.status === 'failed'
                        ? 'bg-danger'
                        : 'bg-line-strong'
                  "
                />
                <span class="shrink-0 tabular-nums">{{ run.at.slice(11, 16) }}</span>
                <span class="min-w-0 flex-1 truncate" :title="run.message">{{ run.message }}</span>
              </li>
            </ul>
          </section>

          <div class="mt-5 rounded-control border border-line-soft bg-canvas/60 px-3 py-2">
            <p class="text-caption leading-relaxed text-muted">{{ t("activity.officialDisclaimer") }}</p>
            <a
              v-if="officialUrl"
              :href="officialUrl"
              target="_blank"
              rel="noreferrer"
              class="mt-1.5 inline-flex items-center gap-1 text-caption text-brand hover:underline"
            >
              {{ campaign.officialLabel }}
              <ExternalLink :size="9" :stroke-width="2" />
            </a>
          </div>
        </div>
      </aside>
    </Transition>
  </Teleport>
</template>

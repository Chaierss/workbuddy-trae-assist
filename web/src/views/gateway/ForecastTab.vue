<script setup lang="ts">
import { computed, onMounted, ref, type Component } from "vue";
import {
  Activity,
  KeyRound,
  Layers,
  Minus,
  RefreshCw,
  TrendingDown,
  TrendingUp,
} from "lucide-vue-next";
import SectionCard from "../../components/SectionCard.vue";
import StatCard from "../../components/StatCard.vue";
import StatusPill from "../../components/StatusPill.vue";
import {
  api,
  formatAmount,
  type GatewayForecast,
  type GatewayForecastAccount,
  type GatewayForecastKey,
} from "../../api";
import { t, type MessageKey } from "../../lib/i18n";
import { compact } from "./useGateway";

/**
 * 用量预测：只读日账本与积分快照。
 *
 * 三条口径直接写在页面上：均值只用完整自然日（不含今天）、至少 2 个活跃日才给数字、
 * 积分耗尽日只在真实快照下降时才计算。数据不够就显示「数据不足」，不替用户猜。
 */
const forecast = ref<GatewayForecast | null>(null);
const loading = ref(false);
const error = ref("");

async function load(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    forecast.value = await api.gatewayForecast(14);
  } catch (cause) {
    error.value = (cause as Error).message;
  } finally {
    loading.value = false;
  }
}

onMounted(load);

const totals = computed(() => forecast.value?.totals ?? null);
const series = computed(() => forecast.value?.series ?? []);
const accounts = computed(() => forecast.value?.accounts ?? []);
const keys = computed(() => forecast.value?.keys ?? []);

const maxTokens = computed(() =>
  Math.max(1, ...series.value.map((point) => point.tokens)),
);
const hasSeries = computed(() => series.value.some((point) => point.tokens > 0));

/** 竖向柱高：72px 绘图区，至少 3px，否则 0 与个位数分不开。 */
function barHeight(value: number): string {
  return `${Math.max(3, Math.round((value / maxTokens.value) * 72))}px`;
}

function dayLabel(value: string): string {
  return value.slice(5);
}

function tokensText(value: number | null): string {
  return value === null ? t("gateway.forecast.insufficientShort") : compact(value);
}

/** 比例（0..1）转百分比文案；没有限额时如实说「未设限额」。 */
function percentText(value: number | null): string {
  return value === null ? t("gateway.forecast.noLimit") : `${Math.round(value * 100)}%`;
}

function percentWidth(value: number | null): string {
  return `${Math.min(100, Math.round((value ?? 0) * 100))}%`;
}

type Tone = "neutral" | "brand" | "warn" | "danger" | "info";

const STATE_TEXT_KEY: Record<string, MessageKey> = {
  usable: "gateway.state.usable",
  cooling: "gateway.state.cooling",
  exhausted: "gateway.state.exhausted",
  excluded: "gateway.state.excluded",
  unavailable: "gateway.state.unavailable",
};

const STATE_TONE: Record<string, Tone> = {
  usable: "brand",
  cooling: "warn",
  exhausted: "danger",
  excluded: "neutral",
  unavailable: "neutral",
};

const TREND_ICON = { up: TrendingUp, down: TrendingDown, stable: Minus };
const TREND_KEY: Record<string, MessageKey> = {
  up: "gateway.forecast.trendUp",
  down: "gateway.forecast.trendDown",
  stable: "gateway.forecast.trendStable",
};

const CONFIDENCE_KEY: Record<string, MessageKey> = {
  insufficient: "gateway.forecast.confidenceInsufficient",
  medium: "gateway.forecast.confidenceMedium",
  high: "gateway.forecast.confidenceHigh",
};

const PACE_TEXT_KEY: Record<string, MessageKey> = {
  over: "gateway.forecast.paceOver",
  watch: "gateway.forecast.paceWatch",
  ok: "gateway.forecast.paceOk",
};

const PACE_TONE: Record<string, Tone> = { over: "danger", watch: "warn", ok: "brand" };

function stateText(state: string): string {
  const key = STATE_TEXT_KEY[state];
  return key ? t(key) : state;
}

function stateTone(state: string): Tone {
  return STATE_TONE[state] ?? "neutral";
}

function confidenceText(value: string | undefined): string {
  const key = value ? CONFIDENCE_KEY[value] : undefined;
  return key ? t(key) : t("gateway.forecast.confidenceInsufficient");
}

/** 余额耗尽日：≤2 天危险、≤7 天警告；没有快照就是「数据不足」。 */
function daysTone(row: GatewayForecastAccount): Tone {
  if (row.daysLeft === null) return "neutral";
  if (row.daysLeft <= 2) return "danger";
  if (row.daysLeft <= 7) return "warn";
  return "brand";
}

function daysText(row: GatewayForecastAccount): string {
  if (row.daysLeft === null) return t("gateway.forecast.insufficientShort");
  return t("gateway.forecast.daysLeft", { days: row.daysLeft });
}

function keyLimitText(row: GatewayForecastKey): string {
  if (!row.tokenLimit && !row.requestLimit) return t("gateway.forecast.noLimit");
  return t("gateway.forecast.keyLimit", {
    tokens: row.tokenLimit ? compact(row.tokenLimit) : "—",
    requests: row.requestLimit ? compact(row.requestLimit) : "—",
  });
}

function trendIcon(trend: string): Component {
  return TREND_ICON[trend as keyof typeof TREND_ICON] ?? Minus;
}

function trendText(trend: string): string {
  const key = TREND_KEY[trend];
  return key ? t(key) : trend;
}

function paceText(pace: string): string {
  const key = PACE_TEXT_KEY[pace];
  return key ? t(key) : pace;
}

function paceTone(pace: string): Tone {
  return PACE_TONE[pace] ?? "neutral";
}
</script>

<template>
  <div>
    <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
      <span class="text-caption text-muted">{{ t("gateway.forecast.readonly") }}</span>
      <div class="flex items-center gap-2">
        <StatusPill v-if="totals" :tone="totals.confidence === 'insufficient' ? 'warn' : 'brand'">
          {{ confidenceText(totals.confidence) }}
        </StatusPill>
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-control border border-line px-2.5 py-2 text-body-sm text-ink-soft transition-colors hover:border-brand/50 hover:text-brand-dark disabled:opacity-60"
          :disabled="loading"
          @click="load"
        >
          <RefreshCw :size="13" :stroke-width="1.9" :class="loading ? 'animate-spin' : ''" />
          {{ t("gateway.forecast.refresh") }}
        </button>
      </div>
    </div>

    <div
      v-if="error"
      class="mb-3 rounded-control bg-danger-soft px-3 py-2 text-caption text-danger"
    >
      {{ error }}
    </div>

    <div v-if="!forecast" class="py-8 text-center text-body-sm text-muted">
      {{ loading ? t("gateway.forecast.loading") : t("gateway.forecast.empty") }}
    </div>

    <div v-else>
      <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          :label="t('gateway.forecast.tokensWindow', { days: forecast.window })"
          :value="compact(totals?.tokensWindow ?? 0)"
          :icon="Activity"
          :hint="t('gateway.forecast.requestsWindow', { count: totals?.requestsWindow ?? 0 })"
        />
        <StatCard
          :label="t('gateway.forecast.tokensPerDay')"
          :value="tokensText(totals?.tokensPerDay ?? null)"
          :icon="TrendingUp"
          :hint="confidenceText(totals?.confidence)"
        />
        <StatCard
          :label="t('gateway.forecast.activeKeys')"
          :value="`${totals?.activeKeys ?? 0}`"
          :icon="KeyRound"
          :hint="t('gateway.forecast.keysTotal', { count: keys.length })"
        />
        <StatCard
          :label="t('gateway.forecast.topModel')"
          :value="totals?.topModel ?? t('gateway.forecast.insufficientShort')"
          :icon="Layers"
          :hint="t('gateway.forecast.activeDays', {
            days: totals?.activeDays ?? 0,
            window: forecast.window,
          })"
        />
      </div>

      <div
        v-if="totals?.confidence === 'insufficient'"
        class="mt-4 rounded-control border border-warn/30 bg-warn-soft px-3.5 py-2.5 text-caption leading-relaxed text-warn"
      >
        {{ t("gateway.forecast.insufficientHint") }}
      </div>

      <SectionCard
        class="mt-4"
        :title="t('gateway.forecast.seriesTitle')"
        :subtitle="t('gateway.forecast.seriesSubtitle', { days: forecast.days })"
      >
        <div v-if="!hasSeries" class="py-6 text-center text-body-sm text-muted">
          {{ t("gateway.forecast.seriesEmpty") }}
        </div>
        <div v-else class="flex h-[100px] items-end gap-1.5">
          <div
            v-for="point in series"
            :key="point.date"
            class="flex h-full min-w-0 flex-1 flex-col items-center justify-end gap-1"
            :title="
              t('gateway.forecast.seriesPoint', {
                date: point.date,
                requests: point.requests,
                tokens: compact(point.tokens),
              })
            "
          >
            <span
              class="w-full rounded-t-[3px]"
              :class="point.failed ? 'bg-warn' : 'bg-brand'"
              :style="{ height: barHeight(point.tokens) }"
            ></span>
            <span class="truncate text-micro text-muted">{{ dayLabel(point.date) }}</span>
          </div>
        </div>
      </SectionCard>

      <SectionCard
        class="mt-4"
        :title="t('gateway.forecast.accountsTitle')"
        :subtitle="t('gateway.forecast.accountsSubtitle')"
      >
        <div v-if="!accounts.length" class="py-6 text-center text-body-sm text-muted">
          {{ t("gateway.pool.empty") }}
        </div>
        <div v-else class="overflow-x-auto">
          <div
            class="grid min-w-[720px] grid-cols-[minmax(140px,1.4fr)_84px_110px_110px_140px_96px] items-center gap-2 border-b border-line-soft pb-2 text-caption font-medium text-muted"
          >
            <span>{{ t("gateway.forecast.colAccount") }}</span>
            <span>{{ t("gateway.forecast.colState") }}</span>
            <span class="text-right">{{ t("gateway.forecast.colCredits") }}</span>
            <span class="text-right">{{ t("gateway.forecast.colPerDay") }}</span>
            <span class="text-right">{{ t("gateway.forecast.colExhaust") }}</span>
            <span class="text-right">{{ t("gateway.forecast.colTrend") }}</span>
          </div>
          <div
            v-for="row in accounts"
            :key="row.accountId"
            class="grid min-w-[720px] grid-cols-[minmax(140px,1.4fr)_84px_110px_110px_140px_96px] items-center gap-2 border-b border-line-soft py-2.5 text-body-sm last:border-0"
          >
            <span class="truncate font-medium text-ink" :title="row.name">{{ row.name }}</span>
            <span>
              <StatusPill :tone="stateTone(row.state)">{{ stateText(row.state) }}</StatusPill>
            </span>
            <span class="text-right tabular-nums text-ink-soft">
              {{
                row.credits === null
                  ? t("gateway.forecast.insufficientShort")
                  : formatAmount(row.credits)
              }}
            </span>
            <span class="text-right tabular-nums text-ink-soft">
              {{ tokensText(row.tokensPerDay) }}
            </span>
            <span class="flex flex-col items-end gap-0.5">
              <StatusPill :tone="daysTone(row)">{{ daysText(row) }}</StatusPill>
              <span v-if="row.exhaustDate" class="text-micro text-muted">{{ row.exhaustDate }}</span>
            </span>
            <span class="flex items-center justify-end gap-1 text-caption text-ink-soft">
              <component :is="trendIcon(row.trend)" :size="12" :stroke-width="1.9" />
              {{ trendText(row.trend) }}
            </span>
          </div>
        </div>
        <div class="mt-3 border-t border-line-soft pt-3 text-caption leading-relaxed text-muted">
          {{ t("gateway.forecast.accountsNote") }}
        </div>
      </SectionCard>

      <SectionCard
        class="mt-4"
        :title="t('gateway.forecast.keysTitle')"
        :subtitle="t('gateway.forecast.keysSubtitle')"
      >
        <div v-if="!keys.length" class="py-6 text-center text-body-sm text-muted">
          {{ t("gateway.byKey.empty") }}
        </div>
        <ul v-else class="divide-y divide-line-soft">
          <li
            v-for="row in keys"
            :key="row.keyId"
            class="flex flex-wrap items-center gap-3 py-3 text-body-sm"
          >
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2">
                <span class="truncate font-medium text-ink">{{ row.label }}</span>
                <StatusPill v-if="!row.active" tone="neutral">
                  {{ t("gateway.byKey.revoked") }}
                </StatusPill>
              </div>
              <div class="mt-0.5 truncate font-mono text-caption text-muted">{{ row.mask }}</div>
            </div>

            <div class="w-[220px] shrink-0">
              <div class="mb-1 flex items-center justify-between text-caption text-muted">
                <span>{{ t("gateway.forecast.keyToday") }}</span>
                <span class="tabular-nums">
                  {{ compact(row.todayTokens) }} · {{ percentText(row.todayPercent) }}
                </span>
              </div>
              <div class="h-2 overflow-hidden rounded-full bg-line-soft">
                <span
                  class="block h-full rounded-full"
                  :class="
                    row.pace === 'over' ? 'bg-danger' : row.pace === 'watch' ? 'bg-warn' : 'bg-brand'
                  "
                  :style="{ width: percentWidth(row.todayPercent) }"
                ></span>
              </div>
            </div>

            <div class="w-[130px] shrink-0 text-right text-caption text-muted">
              {{ keyLimitText(row) }}
            </div>
            <div class="w-[110px] shrink-0 text-right text-caption text-muted">
              {{ t("gateway.forecast.keyAvg", { tokens: tokensText(row.avgTokensPerDay) }) }}
            </div>
            <StatusPill :tone="paceTone(row.pace)">{{ paceText(row.pace) }}</StatusPill>
          </li>
        </ul>
        <div class="mt-3 border-t border-line-soft pt-3 text-caption leading-relaxed text-muted">
          {{ t("gateway.forecast.keysNote") }}
        </div>
      </SectionCard>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { Activity, KeyRound, Radar, ScrollText, Users } from "lucide-vue-next";
import SectionCard from "../../components/SectionCard.vue";
import SelectMenu from "../../components/SelectMenu.vue";
import StatCard from "../../components/StatCard.vue";
import StatusPill from "../../components/StatusPill.vue";
import { formatAmount, formatTime, type GatewayPoolAccount } from "../../api";
import { t, type MessageKey } from "../../lib/i18n";
import { compact, useGateway } from "./useGateway";

/** 概览：总体指标 + 账号池（排除 / 偏好 / 探测）+ 用量看板 + 小时趋势。 */
const store = useGateway();

const stats = computed(() => store.status?.stats ?? null);
const board = computed(() => stats.value?.breakdown ?? null);
const catalogModels = computed(() => store.status?.catalog.models ?? []);
const probeOptions = computed(() =>
  catalogModels.value.map((model) => ({ value: model.id, label: model.id })),
);

/** 账号池：可用 / 冷却中 / 积分耗尽 / 已排除；人工处置与探测都在这一区。 */
const pool = computed(() => store.pool);

/** 账号池状态的中文文案存 key：常量在模块级，直接调 t() 切语言不会更新。 */
const STATE_TEXT_KEY: Record<string, MessageKey> = {
  usable: "gateway.state.usable",
  cooling: "gateway.state.cooling",
  exhausted: "gateway.state.exhausted",
  excluded: "gateway.state.excluded",
  unavailable: "gateway.state.unavailable",
};

type Tone = "neutral" | "brand" | "warn" | "danger" | "info";

const STATE_TONE: Record<string, Tone> = {
  usable: "brand",
  cooling: "warn",
  exhausted: "danger",
  excluded: "neutral",
  unavailable: "neutral",
};

function stateText(state: string): string {
  const key = STATE_TEXT_KEY[state];
  return key ? t(key) : state;
}

function stateTone(state: string): Tone {
  return STATE_TONE[state] ?? "neutral";
}

/** 每行右侧的补充说明：冷却剩余 / 积分余额（查不到就如实说查不到，不能显示成 0）。 */
function accountHint(item: GatewayPoolAccount): string {
  if (item.state === "cooling")
    return t("gateway.pool.cooldown", { seconds: item.cooldownSeconds });
  const credits = item.credits;
  if (!credits) return t("gateway.pool.creditsUnqueried");
  if (!credits.available) return credits.error || t("gateway.pool.creditsError");
  if (credits.credits === null) return t("gateway.pool.creditsUnknown");
  return t("gateway.pool.creditsRemaining", {
    credits: formatAmount(credits.credits),
    total: credits.total !== null ? formatAmount(credits.total) : "?",
  });
}

/** 条形图宽度基准：取最大值，避免小流量被压成一条线。 */
const maxModelRequests = computed(() =>
  Math.max(1, ...(board.value?.byModel.map((item) => item.requests) ?? [1])),
);
const maxAccountRequests = computed(() =>
  Math.max(1, ...(board.value?.byAccount.map((item) => item.requests) ?? [1])),
);
const maxBucketRequests = computed(() =>
  Math.max(1, ...(board.value?.series.map((item) => item.requests) ?? [1])),
);
const maxKeyRequests = computed(() =>
  Math.max(1, ...(board.value?.byKey.map((item) => item.requests) ?? [1])),
);

/** 按 API Key 的**累计**用量：直接取 Key 记录上的计数器（含已吊销的 Key）。 */
const keyRows = computed(() =>
  [...store.keys]
    .sort((a, b) => b.requests - a.requests)
    .map((key) => ({
      key,
      requests: key.requests,
      tokens: key.tokens,
      // 单次平均 tokens：一眼看出哪把 Key 在跑长上下文。
      avgTokens: key.requests ? Math.round(key.tokens / key.requests) : 0,
    })),
);

const successText = computed(() => {
  const rate = board.value?.successRate;
  if (rate === null || rate === undefined) return "—";
  return `${(rate * 100).toFixed(1)}%`;
});

function percent(value: number, base: number): string {
  return `${Math.max(2, Math.round((value / base) * 100))}%`;
}

/** 竖向柱子高度：按 64px 绘图区换算成 px（至少 3px，否则 1 次请求看不见）。 */
const SERIES_PLOT_PX = 64;
function barHeight(value: number, base: number): string {
  return `${Math.max(3, Math.round((value / base) * SERIES_PLOT_PX))}px`;
}

function bucketLabel(bucket: string): string {
  const iso = bucket.replace("Z", "");
  const match = iso.match(/T(\d{2}):(\d{2})/);
  return match ? `${match[1]}:${match[2]}` : bucket;
}
</script>

<template>
  <div v-if="!store.status" class="py-8 text-center text-body-sm text-muted">
    {{ t("gateway.loading") }}
  </div>

  <div v-else>
    <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <StatCard
        :label="t('gateway.stat.requests')"
        :value="compact(store.status.stats.requests)"
        :icon="Activity"
        :hint="t('gateway.stat.failed', { count: store.status.stats.errors })"
      />
      <StatCard
        :label="t('gateway.stat.tokens')"
        :value="compact(store.status.stats.totalTokens)"
        :icon="ScrollText"
        :hint="
          t('gateway.stat.tokenSplit', {
            prompt: compact(store.status.stats.promptTokens),
            completion: compact(store.status.stats.completionTokens),
          })
        "
      />
      <StatCard
        :label="t('gateway.stat.activeKeys')"
        :value="`${store.activeKeys.length}`"
        :icon="KeyRound"
        :hint="t('gateway.stat.revoked', { count: store.revokedKeys.length })"
      />
      <StatCard
        :label="t('gateway.stat.forwardAccounts')"
        :value="`${store.status.accounts}`"
        :icon="Users"
        :hint="t('gateway.stat.healthyHint')"
      />
    </div>

    <SectionCard
      class="mt-4"
      :title="t('gateway.pool.title')"
      :subtitle="t('gateway.pool.subtitle')"
    >
      <template #actions>
        <SelectMenu
          v-model="store.probeModel"
          :options="probeOptions"
          :placeholder="t('gateway.pool.probeModelOption')"
          size="sm"
          class="w-[190px]"
        />
        <StatusPill :tone="pool?.preferred ? 'brand' : 'neutral'">
          {{ pool?.preferred ? t("gateway.pool.preferredSet") : t("gateway.pool.preferredUnset") }}
        </StatusPill>
      </template>

      <div v-if="!pool?.accounts.length" class="py-6 text-center text-body-sm text-muted">
        {{ t("gateway.pool.empty") }}
      </div>
      <ul v-else class="divide-y divide-line-soft">
        <li
          v-for="item in pool.accounts"
          :key="item.id"
          class="flex flex-wrap items-center gap-3 py-3 text-body-sm"
        >
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2">
              <span class="truncate font-medium text-ink">{{ item.name }}</span>
              <StatusPill :tone="stateTone(item.state)">{{ stateText(item.state) }}</StatusPill>
              <StatusPill v-if="item.preferred" tone="info">{{ t("gateway.pool.preferredBadge") }}</StatusPill>
            </div>
            <div class="mt-0.5 truncate text-caption text-muted">{{ accountHint(item) }}</div>
          </div>

          <button
            type="button"
            class="shrink-0 inline-flex items-center gap-1 rounded-control border border-line px-2.5 py-1.5 text-caption text-ink-soft transition-colors hover:border-brand/50 hover:text-brand-dark disabled:opacity-60"
            :disabled="store.busyAccount === item.id || !store.probeModel"
            @click="store.probeAccount(item)"
          >
            <Radar :size="13" :stroke-width="1.9" />
            {{ t("gateway.pool.probe") }}
          </button>
          <button
            type="button"
            class="shrink-0 rounded-control border border-line px-2.5 py-1.5 text-caption transition-colors disabled:opacity-60"
            :class="
              item.preferred
                ? 'text-brand-dark hover:border-brand/50'
                : 'text-ink-soft hover:border-info/50 hover:text-info'
            "
            :disabled="store.busyAccount === item.id || item.excluded"
            :title="
              item.excluded
                ? t('gateway.pool.preferredDisabledTitle')
                : t('gateway.pool.preferredHintTitle')
            "
            @click="store.updatePoolAccount(item, { preferred: !item.preferred })"
          >
            {{ item.preferred ? t("gateway.pool.unsetPreferred") : t("gateway.pool.setPreferred") }}
          </button>
          <button
            type="button"
            class="shrink-0 rounded-control border border-line px-2.5 py-1.5 text-caption transition-colors disabled:opacity-60"
            :class="
              item.excluded
                ? 'text-ink-soft hover:border-brand/50 hover:text-brand-dark'
                : 'text-muted hover:border-warn/50 hover:text-warn'
            "
            :disabled="store.busyAccount === item.id"
            @click="store.updatePoolAccount(item, { excluded: !item.excluded })"
          >
            {{ item.excluded ? t("gateway.pool.restore") : t("gateway.pool.exclude") }}
          </button>
        </li>
      </ul>

      <div
        v-if="store.probeResult"
        class="mt-3 rounded-control border px-3.5 py-3 text-body-sm"
        :class="
          store.probeResult.ok
            ? 'border-brand/30 bg-brand-soft text-brand-dark'
            : 'border-danger/30 bg-danger-soft text-danger'
        "
      >
        {{
          t("gateway.pool.probeResultPrefix", {
            name: store.probeResult.accountName,
            model: store.probeResult.model,
          })
        }}
        <template v-if="store.probeResult.ok">{{ t("gateway.pool.probeOk") }}</template>
        <template v-else>
          {{
            t("gateway.pool.probeFailed", {
              status: store.probeResult.status || t("gateway.pool.probeNotSent"),
            })
          }}{{ store.probeResult.error }}
        </template>
      </div>

      <div class="mt-3 border-t border-line-soft pt-3 text-caption leading-relaxed text-muted">
        <p>{{ t("gateway.pool.note.prefer") }}</p>
        <p class="mt-1">
          {{ t("gateway.pool.note.excludePrefix") }}<span class="text-ink-soft">{{
            t("gateway.pool.note.excludeEmphasis")
          }}</span>{{ t("gateway.pool.note.excludeSuffix") }}
        </p>
        <p class="mt-1">
          {{ t("gateway.pool.note.probe") }}
        </p>
      </div>
    </SectionCard>

    <SectionCard
      class="mt-4"
      :title="t('gateway.board.title')"
      :subtitle="t('gateway.board.subtitle', { scope: board?.scope ?? 200 })"
    >
      <template #actions>
        <StatusPill :tone="board?.total ? 'brand' : 'neutral'">
          {{ t("gateway.board.samples", { count: board?.total ?? 0 }) }}
        </StatusPill>
      </template>

      <div class="grid gap-4 lg:grid-cols-3">
        <div class="rounded-control border border-line-soft px-3.5 py-3">
          <div class="text-body-sm font-medium text-ink">{{ t("gateway.board.successRate") }}</div>
          <div class="mt-1 text-metric font-semibold text-ink">{{ successText }}</div>
          <div class="mt-0.5 text-caption text-muted">
            {{
              t("gateway.board.hops", {
                hops: board?.totalHops ?? 0,
                avg: board?.avgDurationMs ?? "—",
              })
            }}
          </div>
          <div class="mt-2 text-caption text-muted">
            {{ t("gateway.board.sampledAt", { time: formatTime(board?.sampledAt ?? null) }) }}
          </div>
        </div>

        <div class="rounded-control border border-line-soft px-3.5 py-3 lg:col-span-2">
          <div class="mb-2 text-body-sm font-medium text-ink">{{ t("gateway.board.byModel") }}</div>
          <div v-if="!board?.byModel.length" class="py-3 text-caption text-muted">
            {{ t("gateway.board.noData") }}
          </div>
          <div v-else class="space-y-2">
            <div v-for="item in board.byModel" :key="item.model" class="flex items-center gap-2">
              <span class="w-[150px] shrink-0 truncate font-mono text-caption text-ink-soft" :title="item.model">
                {{ item.model }}
              </span>
              <span class="h-2 min-w-0 flex-1 overflow-hidden rounded-full bg-line-soft">
                <span
                  class="block h-full rounded-full"
                  :class="item.errors ? 'bg-warn' : 'bg-brand'"
                  :style="{ width: percent(item.requests, maxModelRequests) }"
                ></span>
              </span>
              <span class="w-[110px] shrink-0 text-right text-caption text-muted">
                {{
                  t("gateway.board.modelRow", {
                    requests: item.requests,
                    tokens: compact(item.tokens),
                  })
                }}
                <span v-if="item.errors" class="text-warn">{{
                  t("gateway.board.failedSuffix", { count: item.errors })
                }}</span>
              </span>
            </div>
          </div>
        </div>
      </div>

      <div class="mt-4 grid gap-4 lg:grid-cols-2">
        <div class="rounded-control border border-line-soft px-3.5 py-3">
          <div class="mb-2 text-body-sm font-medium text-ink">{{ t("gateway.board.byAccount") }}</div>
          <div v-if="!board?.byAccount.length" class="py-3 text-caption text-muted">
            {{ t("gateway.board.noData") }}
          </div>
          <div v-else class="space-y-2">
            <div
              v-for="item in board.byAccount"
              :key="item.accountId || item.accountName"
              class="flex items-center gap-2"
            >
              <span class="w-[96px] shrink-0 truncate text-caption text-ink-soft" :title="item.accountName">
                {{ item.accountName }}
              </span>
              <span class="h-2 min-w-0 flex-1 overflow-hidden rounded-full bg-line-soft">
                <span
                  class="block h-full rounded-full"
                  :class="item.failed ? 'bg-warn' : 'bg-brand'"
                  :style="{ width: percent(item.requests, maxAccountRequests) }"
                ></span>
              </span>
              <span class="w-[120px] shrink-0 text-right text-caption text-muted">
                {{
                  t("gateway.board.accountRow", {
                    requests: item.requests,
                    ok: item.ok,
                  })
                }}
                <span v-if="item.failed" class="text-danger">{{
                  t("gateway.board.accountFailed", { count: item.failed })
                }}</span>
              </span>
            </div>
          </div>
        </div>

        <div class="rounded-control border border-line-soft px-3.5 py-3">
          <div class="mb-2 text-body-sm font-medium text-ink">{{ t("gateway.board.series") }}</div>
          <div v-if="!board?.series.length" class="py-3 text-caption text-muted">
            {{ t("gateway.board.noData") }}
          </div>
          <div v-else class="flex h-[84px] items-end gap-1.5">
            <div
              v-for="item in board.series"
              :key="item.bucket"
              class="flex h-full min-w-0 flex-1 flex-col items-center justify-end gap-1"
              :title="
                t('gateway.board.bucketTitle', {
                  time: bucketLabel(item.bucket),
                  requests: item.requests,
                  tokens: item.tokens,
                })
              "
            >
              <span
                class="w-full rounded-t-[3px]"
                :class="item.errors ? 'bg-warn' : 'bg-brand'"
                :style="{ height: barHeight(item.requests, maxBucketRequests) }"
              ></span>
              <span class="truncate text-micro text-muted">{{ bucketLabel(item.bucket) }}</span>
            </div>
          </div>
        </div>
      </div>

      <div class="mt-4 rounded-control border border-line-soft px-3.5 py-3">
        <div class="mb-2 text-body-sm font-medium text-ink">{{ t("gateway.board.byKey") }}</div>
        <div v-if="!board?.byKey.length" class="py-3 text-caption text-muted">
          {{ t("gateway.board.noData") }}
        </div>
        <div v-else class="space-y-2">
          <div v-for="item in board.byKey" :key="item.keyId || item.keyLabel" class="flex items-center gap-2">
            <span
              class="w-[150px] shrink-0 truncate text-caption text-ink-soft"
              :title="item.keyLabel"
            >
              {{ item.keyLabel }}
            </span>
            <span class="h-2 min-w-0 flex-1 overflow-hidden rounded-full bg-line-soft">
              <span
                class="block h-full rounded-full"
                :class="item.failed ? 'bg-warn' : 'bg-brand'"
                :style="{ width: percent(item.requests, maxKeyRequests) }"
              ></span>
            </span>
            <span class="w-[120px] shrink-0 text-right text-caption text-muted">
              {{ t("gateway.board.keyRow", { requests: item.requests, ok: item.ok }) }}
              <span v-if="item.failed" class="text-danger">{{
                t("gateway.board.accountFailed", { count: item.failed })
              }}</span>
            </span>
          </div>
        </div>
      </div>
    </SectionCard>

    <SectionCard
      class="mt-4"
      :title="t('gateway.byKey.title')"
      :subtitle="t('gateway.byKey.subtitle')"
    >
      <div v-if="!keyRows.length" class="py-6 text-center text-body-sm text-muted">
        {{ t("gateway.byKey.empty") }}
      </div>
      <ul v-else class="divide-y divide-line-soft">
        <li
          v-for="item in keyRows"
          :key="item.key.id"
          class="flex flex-wrap items-center gap-3 py-3 text-body-sm"
        >
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2">
              <span class="truncate font-medium text-ink">{{ item.key.label }}</span>
              <StatusPill v-if="!item.key.active" tone="neutral">
                {{ t("gateway.byKey.revoked") }}
              </StatusPill>
            </div>
            <div class="mt-0.5 truncate font-mono text-caption text-muted">{{ item.key.mask }}</div>
          </div>
          <div class="shrink-0 text-right text-caption text-ink-soft">
            {{
              t("gateway.byKey.cumulative", {
                requests: item.requests,
                tokens: compact(item.tokens),
              })
            }}
          </div>
          <div class="w-[92px] shrink-0 text-right text-caption text-muted">
            {{ t("gateway.byKey.avgPerRequest", { tokens: compact(item.avgTokens) }) }}
          </div>
          <div class="w-[118px] shrink-0 text-right text-caption text-muted">
            {{
              item.key.dayRequests || item.key.dayTokens
                ? t("gateway.byKey.today", {
                    requests: item.key.dayRequests,
                    tokens: compact(item.key.dayTokens),
                  })
                : t("gateway.byKey.todayNone")
            }}
          </div>
          <button
            type="button"
            class="shrink-0 rounded-control border border-line px-2.5 py-1.5 text-caption text-ink-soft transition-colors hover:border-brand/50 hover:text-brand-dark disabled:opacity-60"
            :disabled="store.keyUsageBusy === item.key.id"
            @click="store.openKeyUsage(item.key)"
          >
            {{ t("gateway.byKey.viewUsage") }}
          </button>
          <div class="w-[150px] shrink-0 text-right text-caption text-muted">
            {{
              item.key.lastUsedAt
                ? t("gateway.byKey.lastUsed", { time: formatTime(item.key.lastUsedAt) })
                : t("gateway.byKey.neverUsed")
            }}
          </div>
        </li>
      </ul>
    </SectionCard>
  </div>
</template>

<script setup lang="ts">
/**
 * 执行记录面板：只列事实（时间 / 活动 / 结论 / 收益），不做推断。
 * 从活动页原来的「最近执行」区块抽出来，独立成标签页后可以给出更完整的列表。
 */
import { computed, ref } from "vue";
import SectionCard from "../SectionCard.vue";
import StatusPill from "../StatusPill.vue";
import { t, type MessageKey } from "../../lib/i18n";
import type { ActivityRun, ActivityToday } from "../../api";

const props = withDefaults(
  defineProps<{
    runs: ActivityRun[];
    /** 账号 id → 显示名；流水只带 id，名字由活动页的号池清单 join 进来。 */
    names?: Record<string, string>;
    /** 今日成效：把跳过拆成四类，回答「今天到底做成了什么」。 */
    today?: ActivityToday | null;
  }>(),
  { runs: () => [], names: () => ({}), today: null },
);

const keyFilter = ref<string>("all");
const accountFilter = ref<string>("all");

const keys = computed(() => {
  const seen = new Set<string>();
  for (const run of props.runs) seen.add(run.key);
  return [...seen].sort();
});

const accountIds = computed(() => {
  const seen = new Set<string>();
  for (const run of props.runs) seen.add(run.accountId);
  return [...seen].sort((a, b) => nameOf(a).localeCompare(nameOf(b)));
});

/** 显示名优先；账号已删或还没加载到清单时回退原始 ID，绝不空白。 */
function nameOf(id: string): string {
  return props.names[id] || id;
}

const filtered = computed(() =>
  props.runs.filter(
    (run) =>
      (keyFilter.value === "all" || run.key === keyFilter.value) &&
      (accountFilter.value === "all" || run.accountId === accountFilter.value),
  ),
);

/** 流水时间戳 → `MM-DD HH:mm`（本地时区）。 */
function dayLabel(value: string): string {
  const moment = new Date(value);
  if (Number.isNaN(moment.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(moment.getMonth() + 1)}-${pad(moment.getDate())} ${pad(moment.getHours())}:${pad(moment.getMinutes())}`;
}
</script>

<template>
  <SectionCard
    :title="t('activity.officialRunsTitle')"
    :subtitle="t('activity.officialRunsSubtitle')"
    flush
  >
    <template #actions>
      <select
        v-if="keys.length > 1"
        v-model="keyFilter"
        class="rounded-control border border-line bg-surface px-2 py-1 text-caption text-ink-soft"
      >
        <option value="all">{{ t("activity.officialRunsAll") }}</option>
        <option v-for="key in keys" :key="key" :value="key">
          {{ props.runs.find((run) => run.key === key)?.title || key }}
        </option>
      </select>
      <select
        v-if="accountIds.length > 1"
        v-model="accountFilter"
        class="rounded-control border border-line bg-surface px-2 py-1 text-caption text-ink-soft"
      >
        <option value="all">{{ t("activity.officialRunsAllAccounts") }}</option>
        <option v-for="id in accountIds" :key="id" :value="id">{{ nameOf(id) }}</option>
      </select>
    </template>

    <div
      v-if="props.today"
      class="grid grid-cols-2 gap-px border-b border-line-soft bg-line-soft sm:grid-cols-5"
    >
      <div
        v-for="cell in [
          { key: 'done', value: props.today.reasons.done, tone: 'text-brand-dark' },
          { key: 'ok', value: props.today.ok, tone: 'text-brand-dark' },
          { key: 'upstream', value: props.today.reasons.upstream, tone: 'text-ink-soft' },
          { key: 'prereq', value: props.today.reasons.prereq, tone: 'text-warn' },
          { key: 'detect', value: props.today.reasons.detect, tone: 'text-danger' },
        ]"
        :key="cell.key"
        class="bg-surface px-5 py-3"
      >
        <div class="text-caption text-muted">
          {{ t(`activity.today.${cell.key}` as MessageKey) }}
        </div>
        <div class="mt-0.5 text-metric font-semibold tabular-nums" :class="cell.tone">
          {{ cell.value }}
        </div>
      </div>
    </div>
    <div
      v-if="props.today"
      class="border-b border-line-soft px-5 py-2 text-caption text-muted"
    >
      {{
        t("activity.today.summary", {
          total: props.today.total,
          failed: props.today.failed,
          reward: props.today.reward,
        })
      }}
    </div>

    <div v-if="!filtered.length" class="px-5 py-10 text-center text-body text-muted">
      {{ t("activity.officialRunsEmpty") }}
    </div>

    <ul v-else class="divide-y divide-line-soft">
      <li
        v-for="run in filtered"
        :key="`${run.accountId}-${run.key}-${run.at}`"
        class="flex flex-wrap items-center gap-x-3 gap-y-1 px-5 py-2.5 text-caption"
      >
        <span class="shrink-0 tabular-nums text-muted">{{ dayLabel(run.at) }}</span>
        <span class="shrink-0 text-ink-soft">{{ run.title || run.key }}</span>
        <StatusPill v-if="run.ended" tone="neutral">{{ t("activity.ended") }}</StatusPill>
        <StatusPill
          :tone="
            run.status === 'ok'
              ? 'brand'
              : run.status === 'failed'
                ? 'danger'
                : run.status === 'unknown'
                  ? 'info'
                  : 'neutral'
          "
        >
          {{
            run.status === "ok"
              ? t("activity.resultOk")
              : run.status === "failed"
                ? t("activity.resultFailed")
                : run.status === "unknown"
                  ? t("activity.resultUnknown")
                  : t("activity.resultSkipped")
          }}
        </StatusPill>
        <span v-if="run.attempt > 1" class="shrink-0 text-caption text-muted">
          {{ t("activity.officialRunsAttempt", { attempt: run.attempt }) }}
        </span>
        <span class="min-w-0 flex-1 truncate text-muted" :title="run.message">{{ run.message }}</span>
        <RouterLink
          :to="{ path: '/accounts', query: { account: run.accountId } }"
          class="shrink-0 truncate text-muted tabular-nums transition-colors hover:text-brand hover:underline"
          :title="t('activity.officialRunsOpenAccount')"
        >
          {{ nameOf(run.accountId) }}
        </RouterLink>
        <span v-if="run.reward" class="shrink-0 tabular-nums text-brand-dark">
          +{{ run.reward }}
        </span>
      </li>
    </ul>
  </SectionCard>
</template>

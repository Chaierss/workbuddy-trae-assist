<script setup lang="ts">
/**
 * 执行记录面板：只列事实（时间 / 活动 / 结论 / 收益），不做推断。
 * 从活动页原来的「最近执行」区块抽出来，独立成标签页后可以给出更完整的列表。
 */
import { computed, ref } from "vue";
import SectionCard from "../SectionCard.vue";
import StatusPill from "../StatusPill.vue";
import { t } from "../../lib/i18n";
import type { ActivityRun } from "../../api";

const props = withDefaults(
  defineProps<{
    runs: ActivityRun[];
  }>(),
  { runs: () => [] },
);

const keyFilter = ref<string>("all");

const keys = computed(() => {
  const seen = new Set<string>();
  for (const run of props.runs) seen.add(run.key);
  return [...seen].sort();
});

const filtered = computed(() =>
  keyFilter.value === "all"
    ? props.runs
    : props.runs.filter((run) => run.key === keyFilter.value),
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
        <option v-for="key in keys" :key="key" :value="key">{{ key }}</option>
      </select>
    </template>

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
        <span class="shrink-0 text-ink-soft">{{ run.key }}</span>
        <StatusPill
          :tone="run.status === 'ok' ? 'brand' : run.status === 'failed' ? 'danger' : 'neutral'"
        >
          {{
            run.status === "ok"
              ? t("activity.resultOk")
              : run.status === "failed"
                ? t("activity.resultFailed")
                : t("activity.resultSkipped")
          }}
        </StatusPill>
        <span v-if="run.attempt > 1" class="shrink-0 text-caption text-muted">
          {{ t("activity.officialRunsAttempt", { attempt: run.attempt }) }}
        </span>
        <span class="min-w-0 flex-1 truncate text-muted" :title="run.message">{{ run.message }}</span>
        <span class="shrink-0 tabular-nums text-muted">{{ run.accountId }}</span>
        <span v-if="run.reward" class="shrink-0 tabular-nums text-brand-dark">
          +{{ run.reward }}
        </span>
      </li>
    </ul>
  </SectionCard>
</template>

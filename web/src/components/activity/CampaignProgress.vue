<script setup lang="ts">
/**
 * 活动级进度条。
 *
 * 用分段条而不是单色条：同一个活动在不同账号上可能是「已完成 / 可执行 / 需手动 /
 * 不可查」四种局面，单色条只能表达「完成多少」，说不清剩下的为什么没完成。
 */
import { computed } from "vue";
import type { ActivityCampaign } from "../../api";
import { t } from "../../lib/i18n";

const props = defineProps<{ campaign: ActivityCampaign }>();

const percent = computed(() =>
  props.campaign.accountsTotal
    ? Math.round((props.campaign.done / props.campaign.accountsTotal) * 100)
    : 0,
);

const segments = computed(() => {
  const total = props.campaign.accountsTotal || 1;
  const rows = [
    { key: "done", count: props.campaign.done, class: "bg-brand" },
    { key: "actionable", count: props.campaign.actionable, class: "bg-warn" },
    { key: "manual", count: props.campaign.manual, class: "bg-line-strong" },
    { key: "unavailable", count: props.campaign.unavailable, class: "bg-danger/60" },
    { key: "notDetected", count: props.campaign.notDetected, class: "bg-line" },
  ];
  return rows
    .filter((row) => row.count > 0)
    .map((row) => ({ ...row, width: `${(row.count / total) * 100}%` }));
});
</script>

<template>
  <div>
    <div class="flex items-baseline justify-between gap-2">
      <span class="text-caption text-muted">
        {{ t("activity.officialProgress", { done: campaign.done, total: campaign.accountsTotal }) }}
      </span>
      <span class="text-body-sm font-medium tabular-nums text-ink">{{ percent }}%</span>
    </div>
    <div class="mt-1.5 flex h-1.5 overflow-hidden rounded-full bg-line-soft">
      <span
        v-for="segment in segments"
        :key="segment.key"
        class="h-full transition-[width] duration-500"
        :class="segment.class"
        :style="{ width: segment.width }"
      />
    </div>
    <div class="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-caption">
      <span v-if="campaign.actionable" class="text-warn">
        {{ t("activity.officialActionable", { count: campaign.actionable }) }}
      </span>
      <span v-if="campaign.manual" class="text-muted">
        {{ t("activity.officialManualCount", { count: campaign.manual }) }}
      </span>
      <span v-if="campaign.unavailable" class="text-danger">
        {{ t("activity.officialUnavailableCount", { count: campaign.unavailable }) }}
      </span>
      <span v-if="campaign.notDetected" class="text-muted">
        {{ t("activity.officialNotDetectedCount", { count: campaign.notDetected }) }}
      </span>
      <span v-if="campaign.allDone" class="text-brand-dark">
        {{ t("activity.officialStatusDone") }}
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { RouterLink } from "vue-router";
import { ArrowRight, RotateCw, X } from "lucide-vue-next";
import StatusPill from "./StatusPill.vue";
import { t, type MessageKey } from "../lib/i18n";
import type { RunReportRow, RunReportView } from "../lib/runReport";
import { scopedPath } from "../stores/app";

/**
 * 执行回执：概览与活动页共用。
 *
 * 只列失败明细（最多 3 条），成功 / 跳过用摘要计数表达 —— 用户要的是
 * 「有没有事要处理」，不是把 N 个账号再抄一遍。
 */
const props = defineProps<{ report: RunReportView; busy?: boolean }>();
const emit = defineEmits<{ close: []; retry: [accountId: string] }>();

const failures = computed(() => props.report.rows.filter((row) => row.status === "failed"));

const CREDENTIAL_LABEL: Record<string, MessageKey> = {
  refreshed: "common.runResult.status.refreshed",
  ok: "common.runResult.status.ok",
  noop: "common.runResult.status.noop",
  unsupported: "common.runResult.status.unsupported",
  failed: "common.runResult.status.failed",
  relogin_required: "common.runResult.status.relogin_required",
};

function rowLabel(row: RunReportRow): string {
  if (props.report.kind === "credential") {
    const key = CREDENTIAL_LABEL[row.title];
    if (key) return t(key);
  }
  return row.title;
}

const title = computed(() =>
  t(
    props.report.kind === "activity"
      ? "common.runResult.activityTitle"
      : "common.runResult.credentialTitle",
  ),
);

const exitTo = computed(() => {
  const raw = props.report.kind === "activity" ? "/activity?tab=runs" : "/accounts?risk=credential";
  const index = raw.indexOf("?");
  const path = index >= 0 ? raw.slice(0, index) : raw;
  const query = index >= 0 ? raw.slice(index) : "";
  return scopedPath(path) + query;
});
</script>

<template>
  <div class="mt-3 rounded-card border border-line bg-surface px-5 py-3">
    <div class="flex flex-wrap items-center gap-2">
      <span class="text-body-sm font-medium text-ink">{{ title }}</span>
      <span class="text-caption text-muted">
        {{ t("common.runResult.total", { count: report.summary.total }) }}
      </span>
      <StatusPill v-if="report.summary.ok" tone="brand">
        {{ t("common.runResult.ok", { count: report.summary.ok }) }}
      </StatusPill>
      <StatusPill v-if="report.summary.skipped" tone="neutral">
        {{ t("common.runResult.skipped", { count: report.summary.skipped }) }}
      </StatusPill>
      <StatusPill v-if="report.summary.failed" tone="danger">
        {{ t("common.runResult.failed", { count: report.summary.failed }) }}
      </StatusPill>
      <button
        type="button"
        class="ml-auto inline-flex shrink-0 items-center gap-1 text-caption text-muted transition-colors hover:text-ink"
        @click="emit('close')"
      >
        <X :size="12" :stroke-width="1.9" />
        {{ t("common.close") }}
      </button>
    </div>

    <div v-if="failures.length" class="mt-1.5 space-y-1">
      <div
        v-for="row in failures.slice(0, 3)"
        :key="row.key"
        class="flex flex-wrap items-center gap-1.5 text-caption text-danger"
      >
        <span class="min-w-0 flex-1 truncate" :title="`${row.name}：${row.message}`">
          {{ row.name }} · {{ rowLabel(row) }}：{{ row.message }}
        </span>
        <button
          v-if="row.retryable && row.accountId"
          type="button"
          class="inline-flex shrink-0 items-center gap-1 rounded-badge border border-line px-1.5 py-0.5 text-caption text-ink-soft transition-colors hover:border-brand hover:text-brand-dark disabled:cursor-not-allowed disabled:opacity-45"
          :disabled="busy"
          @click="emit('retry', row.accountId)"
        >
          <RotateCw :size="10" :stroke-width="2" :class="busy ? 'animate-spin' : ''" />
          {{ t("common.retry") }}
        </button>
      </div>
      <div v-if="failures.length > 3" class="text-caption text-muted">
        {{ t("common.runResult.more", { count: failures.length - 3 }) }}
      </div>
    </div>

    <RouterLink
      :to="exitTo"
      class="mt-2 inline-flex items-center gap-1 text-caption text-muted transition-colors hover:text-brand-dark"
    >
      {{ report.kind === "activity" ? t("common.runResult.viewRuns") : t("common.runResult.goAccounts") }}
      <ArrowRight :size="10" :stroke-width="2" />
    </RouterLink>
  </div>
</template>

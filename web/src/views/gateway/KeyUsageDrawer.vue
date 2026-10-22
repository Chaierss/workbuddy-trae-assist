<script setup lang="ts">
import { computed } from "vue";
import { formatTime } from "../../api";
import { t } from "../../lib/i18n";
import { compact, useGateway } from "./useGateway";

/**
 * 单把 Key 的用量抽屉（Key 页与概览页共用，挂在容器上）。
 *
 * 两个口径必须分开显示：
 * - **累计**：Key 记录上的计数器（跨日志保留期）；
 * - **日志内**：只用当前保留的最近 N 条日志算出来，不能当全量。
 */
const store = useGateway();

const usage = computed(() => store.keyUsage);

const stats = computed(() => {
  const current = usage.value;
  if (!current) return [];
  const { sample, key } = current;
  return [
    { label: t("gateway.keyUsage.lifetime"), value: String(key.requests) },
    { label: t("gateway.keyUsage.lifetimeTokens"), value: compact(key.tokens) },
    { label: t("gateway.keyUsage.sampleRequests"), value: String(sample.requests) },
    { label: t("gateway.keyUsage.sampleTokens"), value: compact(sample.tokens) },
    {
      label: t("gateway.keyUsage.successRate"),
      value: sample.requests ? `${Math.round((sample.ok / sample.requests) * 100)}%` : "—",
    },
    {
      label: t("gateway.keyUsage.avgDuration"),
      value:
        sample.avgDurationMs === null
          ? t("gateway.keyUsage.noDuration")
          : `${sample.avgDurationMs}ms`,
    },
  ];
});

/** 柱高按窗口内峰值归一，至少留 6% 让「有 1 次」也能看见。 */
function barHeight(requests: number): number {
  const series = usage.value?.series ?? [];
  const peak = Math.max(1, ...series.map((point) => point.requests));
  return Math.max(6, Math.round((requests / peak) * 100));
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="usage"
      class="fixed inset-0 z-[400] flex justify-end bg-black/25"
      @click.self="store.closeKeyUsage"
    >
      <aside
        class="relative flex h-full w-full max-w-[620px] flex-col overflow-y-auto border-l border-line bg-surface p-5"
      >
        <div class="flex items-start justify-between gap-3">
          <div class="min-w-0">
            <h3 class="truncate text-[15px] font-semibold text-ink">
              {{ t("gateway.keyUsage.title", { label: usage.key.label }) }}
            </h3>
            <p class="mt-1 text-[11.5px] leading-relaxed text-muted">
              {{ t("gateway.keyUsage.subtitle", { scope: usage.sample.scope }) }}
            </p>
          </div>
          <button
            type="button"
            class="shrink-0 rounded-[9px] border border-line px-2.5 py-1.5 text-[11.5px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink"
            @click="store.closeKeyUsage"
          >
            {{ t("gateway.keyUsage.close") }}
          </button>
        </div>

        <div class="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-3">
          <div
            v-for="stat in stats"
            :key="stat.label"
            class="rounded-[10px] border border-line-soft px-3 py-2"
          >
            <div class="text-[11px] text-muted">{{ stat.label }}</div>
            <div class="mt-0.5 text-[14px] font-semibold text-ink">{{ stat.value }}</div>
          </div>
        </div>

        <div v-if="!usage.sample.requests" class="mt-5 text-[12.5px] text-muted">
          {{ t("gateway.keyUsage.emptyModel") }}
        </div>

        <template v-else>
          <div class="mt-5">
            <div class="text-[12px] font-medium text-ink">{{ t("gateway.keyUsage.byModel") }}</div>
            <table class="mt-2 w-full text-[11.5px]">
              <thead class="text-muted">
                <tr class="border-b border-line-soft">
                  <th class="py-1.5 text-left font-normal">{{ t("gateway.keyUsage.colModel") }}</th>
                  <th class="py-1.5 text-right font-normal">
                    {{ t("gateway.keyUsage.colRequests") }}
                  </th>
                  <th class="py-1.5 text-right font-normal">{{ t("gateway.keyUsage.colTokens") }}</th>
                  <th class="py-1.5 text-right font-normal">{{ t("gateway.keyUsage.colErrors") }}</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="row in usage.byModel"
                  :key="row.model"
                  class="border-b border-line-soft last:border-0"
                >
                  <td class="max-w-[240px] truncate py-1.5 font-mono text-ink" :title="row.model">
                    {{ row.model }}
                  </td>
                  <td class="py-1.5 text-right text-ink-soft">{{ row.requests }}</td>
                  <td class="py-1.5 text-right text-ink-soft">{{ compact(row.tokens) }}</td>
                  <td class="py-1.5 text-right" :class="row.errors ? 'text-warn' : 'text-muted'">
                    {{ row.errors }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="mt-5">
            <div class="text-[12px] font-medium text-ink">{{ t("gateway.keyUsage.byHour") }}</div>
            <div class="mt-2 flex h-16 items-end gap-1">
              <div
                v-for="point in usage.series"
                :key="point.bucket"
                class="flex-1 rounded-t-[3px]"
                :class="point.errors ? 'bg-warn/40' : 'bg-brand/50'"
                :style="{ height: `${barHeight(point.requests)}%` }"
                :title="`${point.bucket} · ${point.requests} · ${compact(point.tokens)} tokens`"
              />
            </div>
          </div>

          <div class="mt-5">
            <div class="text-[12px] font-medium text-ink">{{ t("gateway.keyUsage.recent") }}</div>
            <ul class="mt-2 divide-y divide-line-soft">
              <li
                v-for="entry in usage.recent"
                :key="entry.id"
                class="flex items-center gap-2 py-1.5 text-[11.5px]"
              >
                <span
                  class="h-1.5 w-1.5 shrink-0 rounded-full"
                  :class="entry.ok ? 'bg-brand' : 'bg-warn'"
                />
                <span class="w-28 shrink-0 truncate text-muted">{{ formatTime(entry.at) }}</span>
                <span class="w-36 shrink-0 truncate font-mono text-ink-soft">{{ entry.model }}</span>
                <span
                  class="min-w-0 flex-1 truncate text-muted"
                  :class="entry.promptPreview ? '' : 'italic'"
                  :title="entry.promptPreview || t('gateway.keyUsage.noPrompt')"
                >
                  {{ entry.promptPreview || t("gateway.keyUsage.noPrompt") }}
                </span>
                <span class="shrink-0 text-muted">{{ entry.durationMs }}ms</span>
                <span class="w-16 shrink-0 text-right text-ink">
                  {{ compact(entry.totalTokens) }}
                </span>
              </li>
            </ul>
          </div>
        </template>
      </aside>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { CheckCircle2, CircleAlert, Loader2, RotateCcw, X } from "lucide-vue-next";
import { t } from "../lib/i18n";
import {
  clearFinished,
  retryTask,
  taskDrawerOpen,
  taskItems,
  type TaskStatus,
} from "../stores/tasks";

/**
 * 任务中心抽屉：只看回执，不做进度条。
 * 进行中的条目来自当前进程；刷新后只剩最近 20 条终态回执。
 */
const rows = computed(() =>
  [...taskItems.value].sort((a, b) => {
    const rank: Record<TaskStatus, number> = {
      running: 0,
      queued: 1,
      failed: 2,
      ok: 3,
    };
    const byStatus = rank[a.status] - rank[b.status];
    if (byStatus) return byStatus;
    return b.startedAt - a.startedAt;
  }),
);

function clock(ts: number): string {
  return new Date(ts).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

const STATUS_KEY = {
  queued: "tasks.statusQueued",
  running: "tasks.statusRunning",
  ok: "tasks.statusOk",
  failed: "tasks.statusFailed",
} as const;
</script>

<template>
  <Teleport to="body">
    <div
      class="fixed inset-0 z-[60] bg-ink/20"
      @click.self="taskDrawerOpen = false"
    >
      <aside
        class="absolute inset-y-0 right-0 flex w-full max-w-[420px] flex-col border-l border-line bg-surface shadow-2xl"
      >
        <header class="flex items-start gap-2 border-b border-line-soft px-4 py-3">
          <div class="min-w-0 flex-1">
            <div class="text-body font-semibold text-ink">{{ t("tasks.title") }}</div>
            <div class="mt-0.5 text-caption text-muted">{{ t("tasks.subtitle") }}</div>
          </div>
          <button
            type="button"
            class="grid size-7 shrink-0 place-items-center rounded-control text-muted transition-colors hover:bg-line-soft hover:text-ink"
            :aria-label="t('common.close')"
            @click="taskDrawerOpen = false"
          >
            <X :size="15" :stroke-width="1.9" />
          </button>
        </header>

        <div v-if="!rows.length" class="flex flex-1 items-center justify-center px-6 text-center">
          <p class="text-body-sm leading-relaxed text-muted">{{ t("tasks.empty") }}</p>
        </div>

        <ul v-else class="flex-1 space-y-1.5 overflow-y-auto px-3 py-3">
          <li
            v-for="item in rows"
            :key="item.id"
            class="rounded-control border border-line-soft px-3 py-2"
          >
            <div class="flex items-center gap-2">
              <Loader2
                v-if="item.status === 'running' || item.status === 'queued'"
                :size="13"
                class="shrink-0 animate-spin text-brand"
              />
              <CircleAlert
                v-else-if="item.status === 'failed'"
                :size="13"
                class="shrink-0 text-danger"
              />
              <CheckCircle2 v-else :size="13" class="shrink-0 text-brand" />
              <span class="min-w-0 flex-1 truncate text-body-sm text-ink">{{ item.title }}</span>
              <span class="shrink-0 text-micro tabular-nums text-muted">
                {{ clock(item.startedAt) }}
              </span>
            </div>
            <div class="mt-1 flex items-center gap-2 pl-[21px]">
              <span
                class="text-micro"
                :class="
                  item.status === 'failed'
                    ? 'text-danger'
                    : item.status === 'ok'
                      ? 'text-brand-dark'
                      : 'text-muted'
                "
              >
                {{ t(STATUS_KEY[item.status]) }}
              </span>
              <span
                v-if="item.message"
                class="min-w-0 flex-1 truncate text-micro text-muted"
                :title="item.message"
              >
                {{ item.message }}
              </span>
              <button
                v-if="item.status === 'failed' && 'retry' in item && item.retry"
                type="button"
                class="ml-auto inline-flex shrink-0 items-center gap-1 rounded-badge border border-line px-1.5 py-0.5 text-micro text-ink-soft transition-colors hover:border-line-strong hover:text-ink"
                @click="retryTask(item.id)"
              >
                <RotateCcw :size="10" :stroke-width="2" />
                {{ t("tasks.retry") }}
              </button>
            </div>
          </li>
        </ul>

        <footer v-if="rows.length" class="border-t border-line-soft px-4 py-2.5">
          <button
            type="button"
            class="text-caption text-muted transition-colors hover:text-ink"
            @click="clearFinished"
          >
            {{ t("tasks.clear") }}
          </button>
        </footer>
      </aside>
    </div>
  </Teleport>
</template>

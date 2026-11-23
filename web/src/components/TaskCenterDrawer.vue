<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { CheckCircle2, CircleAlert, Loader2, RotateCcw, X } from "lucide-vue-next";
import { t } from "../lib/i18n";
import { api, type ActivityRun, type ActivityToday } from "../api";
import { accounts } from "../stores/app";
import SegmentedControl from "./SegmentedControl.vue";
import ActivityRunsPanel from "./activity/ActivityRunsPanel.vue";
import {
  clearFinished,
  retryTask,
  taskDrawerOpen,
  taskDrawerTab,
  taskItems,
  type TaskStatus,
} from "../stores/tasks";

/**
 * 任务中心抽屉：回执 + 执行历史两个分段。
 * 进行中的条目来自当前进程；刷新后只剩最近 20 条终态回执。
 * 执行历史复用活动页的流水面板，不再单独占一个活动页签。
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

const TABS = computed(() => [
  { value: "receipt", label: t("tasks.tabReceipt") },
  { value: "history", label: t("tasks.tabHistory") },
]);

const runs = ref<ActivityRun[]>([]);
const today = ref<ActivityToday | null>(null);
const historyBusy = ref(false);
const historyLoaded = ref(false);
const historyError = ref("");
const rerunBusy = ref(false);

/** 流水只带账号 id，名字从号池清单 join 进来；清单没加载到就回退 id。 */
const names = computed<Record<string, string>>(() => {
  const map: Record<string, string> = {};
  for (const account of accounts.value) map[account.id] = account.displayName;
  return map;
});

async function loadHistory(force = false): Promise<void> {
  if (historyBusy.value || (historyLoaded.value && !force)) return;
  historyBusy.value = true;
  historyError.value = "";
  try {
    const [data, summary] = await Promise.all([
      api.activityRuns(7, 200),
      api.activityToday(),
    ]);
    runs.value = data.runs;
    today.value = summary;
    historyLoaded.value = true;
  } catch (cause) {
    historyError.value = (cause as Error).message;
  } finally {
    historyBusy.value = false;
  }
}

async function rerunFailed(): Promise<void> {
  if (rerunBusy.value) return;
  rerunBusy.value = true;
  historyError.value = "";
  try {
    await api.rerunActivities();
    await loadHistory(true);
  } catch (cause) {
    historyError.value = (cause as Error).message;
  } finally {
    rerunBusy.value = false;
  }
}

watch(taskDrawerOpen, (open) => {
  if (open && taskDrawerTab.value === "history") void loadHistory();
});

watch(taskDrawerTab, (tab) => {
  if (tab === "history" && taskDrawerOpen.value) void loadHistory();
});
</script>

<template>
  <Teleport to="body">
    <div
      class="fixed inset-0 z-[60] bg-ink/20"
      @click.self="taskDrawerOpen = false"
    >
      <aside
        class="absolute inset-y-0 right-0 flex w-full max-w-[480px] flex-col border-l border-line bg-surface shadow-2xl"
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

        <div class="border-b border-line-soft px-4 py-2.5">
          <SegmentedControl
            :model-value="taskDrawerTab"
            :options="TABS"
            :label="t('tasks.title')"
            size="sm"
            @update:model-value="taskDrawerTab = $event as 'receipt' | 'history'"
          />
        </div>

        <template v-if="taskDrawerTab === 'receipt'">
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
        </template>

        <template v-else>
          <div class="flex items-center gap-2 px-4 py-2.5">
            <button
              type="button"
              class="inline-flex items-center gap-1.5 rounded-control border border-line bg-surface px-3 py-1.5 text-body-sm text-ink-soft transition-colors hover:border-line-strong hover:text-ink disabled:opacity-60"
              :disabled="rerunBusy"
              :title="t('activity.rerunHint')"
              @click="rerunFailed"
            >
              <Loader2 v-if="rerunBusy" :size="13" :stroke-width="1.9" class="animate-spin" />
              <RotateCcw v-else :size="13" :stroke-width="1.9" />
              {{ rerunBusy ? t("activity.rerunBusy") : t("activity.rerun") }}
            </button>
          </div>

          <div class="flex-1 overflow-y-auto px-3 pb-3">
            <p v-if="historyError" class="px-2 py-2 text-body-sm text-danger">
              {{ historyError }}
            </p>
            <div v-if="historyBusy && !historyLoaded" class="space-y-2 px-2 py-3">
              <div class="h-16 animate-pulse rounded-card bg-line-soft" />
              <div class="h-16 animate-pulse rounded-card bg-line-soft" />
            </div>
            <ActivityRunsPanel
              v-else
              :runs="runs"
              :names="names"
              :today="today"
            />
          </div>
        </template>
      </aside>
    </div>
  </Teleport>
</template>

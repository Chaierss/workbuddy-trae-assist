<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { ChevronLeft, ChevronRight, Loader2, RefreshCw } from "lucide-vue-next";
import { api, formatBytes, type LogEntry, type LogFile, type LogPage } from "../../api";
import SelectMenu from "../../components/SelectMenu.vue";
import { t } from "../../lib/i18n";

/**
 * 运行日志：REST 分页，不做日志流。
 *
 * 筛选只放后端真正支持的项（文件 / 级别 / 关键字 / 分页）。
 * 摆一个后端会忽略的下拉框等于空交互，比没有更糟。
 */
const LEVELS = ["", "DEBUG", "INFO", "WARNING", "ERROR"] as const;

const files = ref<LogFile[]>([]);
const dir = ref("");
const level = ref("");
const keyword = ref("");
const file = ref("");
const page = ref(1);
const pageSize = 50;

const data = ref<LogPage | null>(null);
const loading = ref(false);
const error = ref<string | null>(null);
const expanded = ref<number | null>(null);

let debounce: ReturnType<typeof setTimeout> | null = null;

const entries = computed(() => data.value?.entries ?? []);

// 下拉选项集中在这里：标签走 i18n，值保持后端认识的原始字符串。
const levelOptions = computed(() =>
  LEVELS.map((item) => ({ value: item, label: item || t("settings.logs.levelAll") })),
);
const fileOptions = computed(() => [
  { value: "", label: t("settings.logs.fileAll") },
  ...files.value.map((item) => ({
    value: item.name,
    label: t("settings.logs.fileOption", { name: item.name, size: formatBytes(item.size) }),
  })),
]);

const levelTone: Record<string, string> = {
  ERROR: "bg-danger-soft text-danger",
  WARNING: "bg-warn-soft text-warn",
  INFO: "bg-info-soft text-info",
  DEBUG: "bg-line-soft text-muted",
};

async function load(): Promise<void> {
  loading.value = true;
  error.value = null;
  expanded.value = null;
  try {
    data.value = await api.logs({
      page: page.value,
      pageSize,
      level: level.value || undefined,
      keyword: keyword.value.trim() || undefined,
      file: file.value || undefined,
    });
  } catch (caught) {
    data.value = null;
    error.value = (caught as Error).message;
  } finally {
    loading.value = false;
  }
}

async function loadFiles(): Promise<void> {
  try {
    const result = await api.logFiles();
    files.value = result.files;
    dir.value = result.dir;
  } catch {
    // 文件列表失败不影响主查询（日志目录可能还没创建）。
    files.value = [];
  }
}

watch([level, file], () => {
  page.value = 1;
  void load();
});

// 关键字做 300ms 防抖，避免每敲一个字符打一次后端。
watch(keyword, () => {
  if (debounce) clearTimeout(debounce);
  debounce = setTimeout(() => {
    page.value = 1;
    void load();
  }, 300);
});

function prev(): void {
  if (page.value <= 1) return;
  page.value -= 1;
  void load();
}

function next(): void {
  if (!data.value?.hasMore) return;
  page.value += 1;
  void load();
}

function refresh(): void {
  void loadFiles();
  void load();
}

function formatAt(at: string | null): string {
  return at ?? "—";
}

onMounted(() => {
  void loadFiles();
  void load();
});
</script>

<template>
  <div class="space-y-3">
    <div class="flex flex-wrap items-center gap-2">
      <SelectMenu v-model="level" :options="levelOptions" class="w-[128px]" />
      <SelectMenu v-model="file" :options="fileOptions" class="w-[240px]" />

      <input
        v-model="keyword"
        type="text"
        :placeholder="t('settings.logs.keywordPlaceholder')"
        class="w-[220px] rounded-control border border-line bg-surface px-2.5 py-1.5 text-body-sm text-ink outline-none placeholder:text-muted focus:border-brand"
      />

      <button
        type="button"
        class="inline-flex items-center gap-1.5 rounded-control border border-line px-2 py-1.5 text-caption text-ink-soft transition-colors hover:border-line-strong hover:text-ink"
        @click="refresh"
      >
        <RefreshCw :size="12" :class="loading ? 'animate-spin' : ''" /> {{ t("common.refresh") }}
      </button>
    </div>

    <div class="overflow-hidden rounded-card border border-line bg-surface">
      <div
        v-if="loading && !entries.length"
        class="flex items-center gap-2 px-4 py-10 text-body text-muted"
      >
        <Loader2 :size="14" class="animate-spin" /> {{ t("settings.logs.reading") }}
      </div>
      <p v-else-if="error" class="px-4 py-10 text-body text-danger">{{ error }}</p>
      <p v-else-if="!entries.length" class="px-4 py-10 text-center text-body text-muted">
        {{ t("settings.logs.empty") }}
      </p>

      <ul v-else class="divide-y divide-line-soft">
        <li v-for="(entry, index) in entries" :key="`${entry.file}-${entry.at}-${index}`">
          <button
            type="button"
            class="flex w-full items-start gap-2.5 px-4 py-2 text-left transition-colors hover:bg-line-soft/60"
            @click="expanded = expanded === index ? null : index"
          >
            <span
              class="w-[62px] shrink-0 rounded-badge px-1.5 py-0.5 text-center font-mono text-micro font-semibold"
              :class="levelTone[entry.level] ?? 'bg-line-soft text-muted'"
            >
              {{ entry.level || "LOG" }}
            </span>
            <span class="w-[124px] shrink-0 font-mono text-caption text-muted">
              {{ formatAt(entry.at).slice(5) }}
            </span>
            <span
              class="min-w-0 flex-1 font-mono text-caption text-ink-soft"
              :class="expanded === index ? 'whitespace-pre-wrap break-all' : 'truncate'"
            >
              {{ entry.message }}
            </span>
          </button>
          <div v-if="expanded === index" class="border-t border-line-soft bg-line-soft/40 px-4 py-1.5">
            <span class="font-mono text-micro text-muted">
              {{ entry.file }} · {{ entry.logger || t("settings.logs.unknownLogger") }}
            </span>
          </div>
        </li>
      </ul>
    </div>

    <div class="flex items-center gap-3">
      <span class="text-caption text-muted">
        {{ t("settings.logs.pageInfo", { page, size: pageSize }) }}
      </span>
      <span class="min-w-0 flex-1 truncate font-mono text-caption text-muted" :title="dir">
        {{ dir || t("settings.logs.dirUnknown") }}
      </span>
      <div class="flex items-center gap-1.5">
        <button
          type="button"
          class="inline-flex items-center gap-1 rounded-control border border-line px-2 py-1 text-caption text-ink-soft transition-colors hover:border-line-strong hover:text-ink disabled:opacity-40"
          :disabled="page <= 1 || loading"
          @click="prev"
        >
          <ChevronLeft :size="12" /> {{ t("settings.logs.prev") }}
        </button>
        <button
          type="button"
          class="inline-flex items-center gap-1 rounded-control border border-line px-2 py-1 text-caption text-ink-soft transition-colors hover:border-line-strong hover:text-ink disabled:opacity-40"
          :disabled="!data?.hasMore || loading"
          @click="next"
        >
          {{ t("settings.logs.next") }} <ChevronRight :size="12" />
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { Download, MessageSquareText, Network, Trash2 } from "lucide-vue-next";
import SectionCard from "../../components/SectionCard.vue";
import SelectMenu from "../../components/SelectMenu.vue";
import StatusPill from "../../components/StatusPill.vue";
import { formatTime, type GatewayLogEntry } from "../../api";
import { t } from "../../lib/i18n";
import { useGateway } from "./useGateway";

/**
 * 请求日志：新的在前，保留条数在「服务」里配置（默认 200）。用来判断「这把 Key 现在能不能用」。
 *
 * 可按 API Key 或客户端传入的会话标识筛选；「按 Key 分组」只是把同一批日志换个
 * 排法，不改数据。会话标识是**客户端给的线索**，给才显示，否则如实写「未提供」。
 */
const store = useGateway();

/** 分组开关是本页的临时视图偏好，不进配置。 */
const groupByKey = ref(false);

/** 筛选下拉里能选的 Key：含已吊销的（它们的日志还在）。 */
const keyOptions = computed(() => [
  { value: "", label: t("gateway.logs.filterAllKeys") },
  ...store.keys.map((key) => ({ value: key.id, label: `${key.label}（${key.mask}）` })),
]);

function onKeyFilterChange(value: string): void {
  store.logKeyFilter = value;
  void store.reloadLogs();
}

/** 当前日志里出现过的会话标识，做成 datalist 提示（只提示，不强制）。 */
const sessionOptions = computed(() => [
  ...new Set(store.logs.map((entry) => entry.sessionId).filter(Boolean)),
].sort());

const filtered = computed(() => Boolean(store.logKeyFilter || store.logSessionFilter));

function sessionText(entry: GatewayLogEntry): string {
  return entry.sessionId || t("gateway.logs.sessionUnknown");
}

/** 渲染用的分段：不分组时就是一段（无标题），分组时按 Key 各一段。 */
const sections = computed(() => {
  if (!groupByKey.value) {
    return [{ key: "", label: "", count: store.logs.length, entries: store.logs }];
  }
  const buckets = new Map<string, { label: string; entries: GatewayLogEntry[] }>();
  for (const entry of store.logs) {
    const id = entry.keyId || "__none__";
    const bucket = buckets.get(id) ?? {
      label: entry.keyLabel || t("gateway.logs.noKey"),
      entries: [],
    };
    bucket.entries.push(entry);
    buckets.set(id, bucket);
  }
  return [...buckets.entries()].map(([id, bucket]) => ({
    key: id,
    label: bucket.label,
    count: bucket.entries.length,
    entries: bucket.entries,
  }));
});

function resetFilters(): void {
  store.logKeyFilter = "";
  store.logSessionFilter = "";
  void store.reloadLogs();
}
</script>

<template>
  <SectionCard :title="t('gateway.logs.title')" :subtitle="t('gateway.logs.subtitle')">
    <template #actions>
      <button
        type="button"
        class="rounded-control border px-2.5 py-2 text-body-sm transition-colors"
        :class="
          groupByKey
            ? 'border-brand/50 text-brand-dark'
            : 'border-line text-ink-soft hover:border-brand/50 hover:text-brand-dark'
        "
        @click="groupByKey = !groupByKey"
      >
        {{ t("gateway.logs.groupByKey") }}
      </button>
      <a
        :href="store.logsExportUrl"
        download
        class="inline-flex items-center gap-1.5 rounded-control border border-line px-2.5 py-2 text-body-sm text-ink-soft transition-colors hover:border-brand/50 hover:text-brand-dark"
      >
        <Download :size="13" :stroke-width="1.9" />
        {{ t("gateway.logs.export") }}
      </a>
      <button
        type="button"
        class="inline-flex items-center gap-1.5 rounded-control border border-line px-2.5 py-2 text-body-sm text-ink-soft transition-colors hover:border-danger/50 hover:text-danger"
        @click="store.clearLogs"
      >
        <Trash2 :size="13" :stroke-width="1.9" />
        {{ t("gateway.logs.clear") }}
      </button>
    </template>

    <div class="mb-3 flex flex-wrap items-center gap-2 border-b border-line-soft pb-3">
      <SelectMenu
        :model-value="store.logKeyFilter"
        :options="keyOptions"
        size="sm"
        class="w-[220px]"
        @update:model-value="onKeyFilterChange"
      />

      <input
        v-model="store.logSessionFilter"
        list="gateway-log-sessions"
        class="h-8 w-[220px] rounded-control border border-line bg-surface px-2 font-mono text-body-sm text-ink focus:border-line-strong focus:outline-none"
        :placeholder="t('gateway.logs.filterSessionPlaceholder')"
        @change="store.reloadLogs"
      />
      <datalist id="gateway-log-sessions">
        <option v-for="item in sessionOptions" :key="item" :value="item"></option>
      </datalist>

      <button
        v-if="filtered"
        type="button"
        class="rounded-control border border-line px-2.5 py-2 text-body-sm text-ink-soft transition-colors hover:border-brand/50 hover:text-brand-dark"
        @click="resetFilters"
      >
        {{ t("gateway.logs.filterReset") }}
      </button>
      <span v-if="filtered" class="text-caption text-muted">
        {{ t("gateway.logs.filteredCount", { count: store.logs.length }) }}
      </span>
    </div>

    <div v-if="!store.logs.length" class="py-6 text-center text-body-sm text-muted">
      {{ filtered ? t("gateway.logs.emptyFiltered") : t("gateway.logs.empty") }}
    </div>

    <div v-for="section in sections" v-else :key="section.key">
      <div
        v-if="section.label"
        class="flex items-center gap-2 border-b border-line-soft bg-surface-soft px-1 py-1.5 text-caption font-medium text-ink-soft"
      >
        <span class="truncate">{{ section.label }}</span>
        <span class="text-muted">{{ t("gateway.logs.groupCount", { count: section.count }) }}</span>
      </div>
      <ul class="divide-y divide-line-soft">
        <li v-for="entry in section.entries" :key="entry.id" class="py-3 text-body-sm">
          <div class="flex flex-wrap items-center gap-2">
            <StatusPill :tone="entry.ok ? 'brand' : 'danger'">
              {{
                entry.ok ? `${entry.status}` : t("gateway.logs.statusFailed", { status: entry.status })
              }}
            </StatusPill>
            <span class="font-mono text-body-sm text-ink">{{ entry.model }}</span>
            <StatusPill v-if="entry.stream" tone="info">{{ t("gateway.logs.stream") }}</StatusPill>
            <span v-if="entry.hops" class="text-caption text-warn">
              {{ t("gateway.logs.hops", { count: entry.hops }) }}
            </span>
            <span class="ml-auto shrink-0 text-caption text-muted">
              {{ formatTime(entry.at) }} · {{ entry.durationMs }}ms
            </span>
          </div>
          <div class="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-caption text-muted">
            <span>
              <Network :size="12" :stroke-width="1.9" class="mr-1 inline align-[-1px]" />
              {{ entry.accountName ?? t("gateway.logs.noAccount") }}
            </span>
            <span>{{ t("gateway.logs.keyLabel", { label: entry.keyLabel }) }}</span>
            <span
              class="truncate font-mono"
              :class="entry.sessionId ? '' : 'italic'"
              :title="entry.sessionId || t('gateway.logs.sessionUnknown')"
            >
              {{ t("gateway.logs.sessionLabel", { session: sessionText(entry) }) }}
            </span>
            <span v-if="entry.totalTokens">tokens {{ entry.totalTokens }}</span>
            <span v-if="entry.requestedModel !== entry.model">
              {{ t("gateway.logs.requestedModel", { model: entry.requestedModel }) }}
            </span>
          </div>
          <div v-if="entry.error" class="mt-1.5 text-caption text-danger">{{ entry.error }}</div>
          <div
            v-if="entry.promptPreview"
            class="mt-1.5 flex items-start gap-1.5 text-caption text-muted"
          >
            <MessageSquareText :size="12" :stroke-width="1.9" class="mt-0.5 shrink-0" />
            <span class="min-w-0 break-all" :title="entry.promptPreview">
              {{ entry.promptPreview }}
            </span>
          </div>
          <div v-if="entry.skipped.length" class="mt-1 text-caption text-muted">
            {{ t("gateway.logs.skipped", { items: entry.skipped.join("；") }) }}
          </div>
        </li>
      </ul>
    </div>
  </SectionCard>
</template>

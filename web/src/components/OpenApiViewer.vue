<script setup lang="ts">
import { computed, ref } from "vue";
import { ChevronRight, Search } from "lucide-vue-next";
import EmptyState from "./EmptyState.vue";
import { t } from "../lib/i18n";
import {
  firstMedia,
  flattenSchema,
  groupOperations,
  schemaLabel,
  type OpenApiMediaType,
  type OpenApiOperation,
  type OpenApiSpec,
  type SchemaRow,
} from "../lib/openapi";

/**
 * 接口文档的只读渲染器：把 `/api/openapi.json` 画成「标签 → 接口 → 详情」。
 *
 * 不内嵌 Swagger UI：桌面端没有打包它的静态资源，iframe 只会白屏；
 * 直接读规格渲染，安装包缺什么资源都不影响，规格变了刷新即最新。
 */
const props = defineProps<{ spec: OpenApiSpec | null }>();

const query = ref("");
/** 展开状态用 `METHOD path` 做键，刷新数据后仍然停在原来展开的那几条。 */
const expanded = ref(new Set<string>());

const groups = computed(() => groupOperations(props.spec));

const filtered = computed(() => {
  const keyword = query.value.trim().toLowerCase();
  if (!keyword) return groups.value;
  return groups.value
    .map((group) => ({
      ...group,
      operations: group.operations.filter((entry) => {
        const haystack = [
          entry.method,
          entry.path,
          entry.operation.summary ?? "",
          entry.operation.description ?? "",
          group.tag ?? "",
        ]
          .join(" ")
          .toLowerCase();
        return haystack.includes(keyword);
      }),
    }))
    .filter((group) => group.operations.length > 0);
});

const total = computed(() =>
  filtered.value.reduce((sum, group) => sum + group.operations.length, 0),
);

const methodTone: Record<string, string> = {
  GET: "bg-brand-soft text-brand-dark",
  POST: "bg-info-soft text-info",
  PUT: "bg-warn-soft text-warn",
  PATCH: "bg-warn-soft text-warn",
  DELETE: "bg-danger-soft text-danger",
};

function operationKey(method: string, path: string): string {
  return `${method} ${path}`;
}

function toggle(method: string, path: string): void {
  const key = operationKey(method, path);
  const next = new Set(expanded.value);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  expanded.value = next;
}

function isOpen(method: string, path: string): boolean {
  return expanded.value.has(operationKey(method, path));
}

function mediaRows(media: OpenApiMediaType | null): SchemaRow[] {
  return flattenSchema(props.spec, media?.schema);
}

function mediaLabel(media: OpenApiMediaType | null): string {
  return media ? schemaLabel(props.spec, media.schema) : "any";
}

function requestBodies(operation: OpenApiOperation) {
  return Object.entries(operation.requestBody?.content ?? {}).map(([contentType, media]) => ({
    contentType,
    label: mediaLabel(media),
    rows: mediaRows(media),
  }));
}

function responseList(operation: OpenApiOperation) {
  return Object.entries(operation.responses ?? {}).map(([status, response]) => ({
    status,
    description: response.description ?? "",
    label: mediaLabel(firstMedia(props.spec, response)),
    rows: mediaRows(firstMedia(props.spec, response)),
  }));
}
</script>

<template>
  <div class="flex h-full min-h-0 flex-col">
    <div class="flex shrink-0 items-center gap-2 border-b border-line-soft px-4 py-2">
      <Search :size="13" class="shrink-0 text-muted" />
      <input
        v-model="query"
        type="text"
        :placeholder="t('settings.api.searchPlaceholder')"
        class="w-[260px] rounded-control border border-line bg-surface px-2.5 py-1.5 text-body-sm text-ink outline-none placeholder:text-muted focus:border-brand"
      />
      <span class="ml-auto text-caption text-muted">
        {{ t("settings.api.endpoints", { count: total }) }}
      </span>
    </div>

    <div class="min-h-0 flex-1 overflow-y-auto">
      <EmptyState
        v-if="!filtered.length"
        :title="t('settings.api.empty')"
        :hint="t('settings.api.emptyHint')"
      />

      <section v-for="group in filtered" :key="group.tag ?? '__other__'">
        <header
          class="sticky top-0 z-[1] flex flex-wrap items-baseline gap-x-2 border-b border-line-soft bg-surface/95 px-4 py-2 backdrop-blur"
        >
          <span class="text-body-sm font-semibold text-ink">
            {{ group.tag ?? t("settings.api.otherTag") }}
          </span>
          <span v-if="group.description" class="text-caption text-muted">
            {{ group.description }}
          </span>
          <span class="ml-auto text-caption text-muted">{{ group.operations.length }}</span>
        </header>

        <article
          v-for="entry in group.operations"
          :key="operationKey(entry.method, entry.path)"
          class="border-b border-line-soft last:border-b-0"
        >
          <button
            type="button"
            class="flex w-full items-center gap-2.5 px-4 py-2 text-left transition-colors hover:bg-line-soft/50"
            @click="toggle(entry.method, entry.path)"
          >
            <ChevronRight
              :size="12"
              class="shrink-0 text-muted transition-transform"
              :class="isOpen(entry.method, entry.path) ? 'rotate-90' : ''"
            />
            <span
              class="w-[54px] shrink-0 rounded-badge py-0.5 text-center font-mono text-micro font-semibold"
              :class="methodTone[entry.method] ?? 'bg-line-soft text-ink-soft'"
            >
              {{ entry.method }}
            </span>
            <span class="shrink-0 font-mono text-caption text-ink">{{ entry.path }}</span>
            <span class="min-w-0 flex-1 truncate text-body-sm text-muted">
              {{ entry.operation.summary ?? "" }}
            </span>
            <span
              v-if="entry.operation.deprecated"
              class="shrink-0 rounded-full bg-danger-soft px-2 py-0.5 text-micro font-semibold text-danger"
            >
              {{ t("settings.api.deprecated") }}
            </span>
          </button>

          <div
            v-if="isOpen(entry.method, entry.path)"
            class="space-y-3 border-t border-line-soft bg-line-soft/20 px-6 py-3"
          >
            <p
              v-if="entry.operation.description || entry.operation.summary"
              class="text-body-sm leading-relaxed text-ink-soft"
            >
              {{ entry.operation.description || entry.operation.summary }}
            </p>

            <div v-if="entry.operation.parameters?.length">
              <h4 class="mb-1.5 text-caption font-semibold text-ink">
                {{ t("settings.api.parameters") }}
              </h4>
              <div class="space-y-1">
                <div
                  v-for="param in entry.operation.parameters"
                  :key="`${param.in}:${param.name}`"
                  class="flex flex-wrap items-baseline gap-x-2 text-caption"
                >
                  <code class="font-mono text-ink">{{ param.name }}</code>
                  <span class="font-mono text-micro text-muted">{{ param.in }}</span>
                  <span class="font-mono text-micro text-info">
                    {{ schemaLabel(props.spec, param.schema) }}
                  </span>
                  <span v-if="param.required" class="text-micro text-danger">
                    {{ t("settings.api.required") }}
                  </span>
                  <span v-if="param.description" class="text-muted">{{ param.description }}</span>
                </div>
              </div>
            </div>

            <div v-for="body in requestBodies(entry.operation)" :key="body.contentType">
              <h4 class="mb-1.5 text-caption font-semibold text-ink">
                {{ t("settings.api.requestBody") }}
                <span class="ml-1 font-mono text-micro font-normal text-muted">
                  {{ body.contentType }} · {{ body.label }}
                </span>
                <span
                  v-if="entry.operation.requestBody?.required"
                  class="ml-1 text-micro text-danger"
                >
                  {{ t("settings.api.required") }}
                </span>
              </h4>
              <p v-if="!body.rows.length" class="text-caption text-muted">
                {{ t("settings.api.noFields") }}
              </p>
              <div v-else class="space-y-1">
                <div
                  v-for="row in body.rows"
                  :key="row.field"
                  class="flex items-baseline gap-2 text-caption"
                  :style="{ paddingLeft: `${row.depth * 14}px` }"
                >
                  <code class="font-mono text-ink">{{ row.field }}</code>
                  <span v-if="row.required" class="text-micro text-danger">
                    {{ t("settings.api.required") }}
                  </span>
                  <span class="font-mono text-micro text-info">{{ row.type }}</span>
                  <span v-if="row.description" class="min-w-0 flex-1 truncate text-muted">
                    {{ row.description }}
                  </span>
                </div>
              </div>
            </div>

            <div v-if="responseList(entry.operation).length">
              <h4 class="mb-1.5 text-caption font-semibold text-ink">
                {{ t("settings.api.responses") }}
              </h4>
              <div class="space-y-2">
                <div
                  v-for="response in responseList(entry.operation)"
                  :key="response.status"
                  class="text-caption"
                >
                  <div class="flex flex-wrap items-baseline gap-2">
                    <code
                      class="font-mono font-semibold"
                      :class="response.status.startsWith('2') ? 'text-brand-dark' : 'text-danger'"
                    >
                      {{ response.status }}
                    </code>
                    <span class="text-ink-soft">{{ response.description }}</span>
                    <span class="font-mono text-micro text-info">{{ response.label }}</span>
                  </div>
                  <div
                    v-for="row in response.rows"
                    :key="`${response.status}:${row.field}`"
                    class="flex items-baseline gap-2"
                    :style="{ paddingLeft: `${row.depth * 14}px` }"
                  >
                    <code class="font-mono text-ink">{{ row.field }}</code>
                    <span class="font-mono text-micro text-info">{{ row.type }}</span>
                    <span v-if="row.description" class="min-w-0 flex-1 truncate text-muted">
                      {{ row.description }}
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </article>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { Check, Search, Shuffle } from "lucide-vue-next";
import type { GatewayCatalogModel } from "../../api";
import { t } from "../../lib/i18n";

/**
 * 模型范围选择器（签发新 Key 与编辑已有 Key 共用）。
 *
 * 三段式：`all`（全部模型，目录里新上的自动可用）/ `capability`（按能力，
 * 随目录自动重算）/ `list`（指定模型，静态白名单）。组件只负责「选什么」，
 * 不落盘 —— 值通过 `v-model` 双向绑定给调用方。
 */
const props = defineProps<{
  scope: string;
  capabilities: string[];
  models: string[];
  catalog: GatewayCatalogModel[];
}>();

const emit = defineEmits<{
  "update:scope": [value: string];
  "update:capabilities": [value: string[]];
  "update:models": [value: string[]];
  /** 目录为空时点「获取目录」：由调用方去抓上游（组件自己不发请求）。 */
  refresh: [];
}>();

/** 能力键的展示顺序；与后端 `CAPABILITY_KEYS` 对齐。 */
const CAPABILITIES = ["free", "image", "long_context"] as const;
/** 大上下文的判定门槛（与后端 `LONG_CONTEXT_TOKENS` 对齐）。 */
const LONG_CONTEXT_TOKENS = 128_000;

const search = ref("");

function meets(model: GatewayCatalogModel, capability: string): boolean {
  if (capability === "free") return model.free;
  if (capability === "image") return model.supportsImages;
  if (capability === "long_context") return model.contextWindow >= LONG_CONTEXT_TOKENS;
  return false;
}

/** 命中判定：选中的多个能力是「与」关系（都要满足）。 */
const matched = computed(() => {
  if (props.scope === "capability") {
    if (!props.capabilities.length) return 0;
    return props.catalog.filter((m) => props.capabilities.every((c) => meets(m, c))).length;
  }
  if (props.scope === "list") return props.models.length;
  return props.catalog.length;
});

/** 按能力模式下「当前实际可用」的模型清单：不选任何能力时为空。 */
const capabilityModels = computed(() =>
  props.capabilities.length
    ? props.catalog.filter((m) => props.capabilities.every((c) => meets(m, c)))
    : [],
);

/** 每项能力各自覆盖多少个模型：直接标在 chip 上，选之前也能看出有没有结果。 */
const capabilityCounts = computed(() =>
  Object.fromEntries(
    CAPABILITIES.map((capability) => [
      capability,
      props.catalog.filter((model) => meets(model, capability)).length,
    ]),
  ) as Record<(typeof CAPABILITIES)[number], number>,
);

const visible = computed(() => {
  const keyword = search.value.trim().toLowerCase();
  if (!keyword) return props.catalog;
  // id 与展示名都参与匹配：上游 id 常是缩写，按中文名/全名搜更符合直觉。
  return props.catalog.filter(
    (m) =>
      m.id.toLowerCase().includes(keyword) ||
      (m.name ?? "").toLowerCase().includes(keyword),
  );
});

function setScope(value: string): void {
  emit("update:scope", value);
}

function toggleCapability(capability: string): void {
  emit(
    "update:capabilities",
    props.capabilities.includes(capability)
      ? props.capabilities.filter((c) => c !== capability)
      : [...props.capabilities, capability],
  );
}

function toggleModel(modelId: string): void {
  emit(
    "update:models",
    props.models.includes(modelId)
      ? props.models.filter((m) => m !== modelId)
      : [...props.models, modelId],
  );
}

function selectAll(): void {
  emit("update:models", props.catalog.map((m) => m.id));
}

function selectNone(): void {
  emit("update:models", []);
}

function invert(): void {
  const current = new Set(props.models);
  emit("update:models", props.catalog.filter((m) => !current.has(m.id)).map((m) => m.id));
}

/** 按能力批量并入：把满足该能力的模型加进当前选择（不取消已有的）。 */
function addByCapability(capability: string): void {
  const current = new Set(props.models);
  for (const model of props.catalog) {
    if (meets(model, capability)) current.add(model.id);
  }
  emit("update:models", [...current]);
}

const modeOptions = computed(() => [
  { value: "all", label: t("gateway.scope.all") },
  { value: "capability", label: t("gateway.scope.capability") },
  { value: "list", label: t("gateway.scope.list") },
]);
</script>

<template>
  <div class="rounded-[10px] border border-line-soft px-3.5 py-3">
    <!-- 模式三选一 -->
    <div class="flex flex-wrap items-center gap-1.5">
      <button
        v-for="mode in modeOptions"
        :key="mode.value"
        type="button"
        class="rounded-[9px] border px-2.5 py-1 text-[11.5px] transition-colors"
        :class="
          props.scope === mode.value
            ? 'border-brand/40 bg-brand-soft text-brand-dark'
            : 'border-line text-ink-soft hover:border-[#d5d8d3] hover:text-ink'
        "
        @click="setScope(mode.value)"
      >
        {{ mode.label }}
      </button>
      <span class="ml-auto text-[11.5px] text-muted">
        {{ t("gateway.scope.count", { total: props.catalog.length, matched }) }}
      </span>
    </div>

    <!-- 全部：目录新增自动可用 -->
    <p v-if="props.scope === 'all'" class="mt-2 text-[11px] leading-relaxed text-muted">
      {{ t("gateway.scope.allHint") }}
    </p>

    <template v-else-if="props.scope === 'capability'">
      <p class="mt-2 text-[11px] leading-relaxed text-muted">
        {{ t("gateway.scope.capabilityHint") }}
      </p>
      <div class="mt-2 flex flex-wrap gap-1.5">
        <button
          v-for="capability in CAPABILITIES"
          :key="capability"
          type="button"
          class="inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-[11.5px] transition-colors"
          :class="
            props.capabilities.includes(capability)
              ? 'border-brand/40 bg-brand-soft text-brand-dark'
              : 'border-line text-ink-soft hover:border-[#d5d8d3] hover:text-ink'
          "
          @click="toggleCapability(capability)"
        >
          <Check v-if="props.capabilities.includes(capability)" :size="12" :stroke-width="2.4" />
          {{ t(`gateway.scope.cap.${capability}`) }}
          <span
            class="rounded-full bg-black/5 px-1.5 text-[10.5px] tabular-nums dark:bg-white/10"
            >{{ capabilityCounts[capability] }}</span
          >
        </button>
      </div>
      <!-- 结果要看得见：按能力选中后把命中的模型逐个列出来，避免「一片空白」 -->
      <p v-if="!props.capabilities.length" class="mt-2 text-[11px] text-muted">
        {{ t("gateway.scope.capabilityEmpty") }}
      </p>
      <template v-else>
        <div class="mt-2.5 flex items-center gap-1.5 text-[11px] text-muted">
          <span>{{ t("gateway.scope.capabilityModels") }}</span>
        </div>
        <div v-if="capabilityModels.length" class="mt-1 flex flex-wrap gap-1.5">
          <span
            v-for="model in capabilityModels"
            :key="model.id"
            class="rounded-full border border-dashed border-brand/40 bg-brand-soft/60 px-2.5 py-1 text-[11.5px] text-brand-dark"
          >
            {{ model.id }}
          </span>
        </div>
        <p v-else class="mt-1 text-[11px] text-warn">
          {{ t("gateway.scope.noMatch") }}
        </p>
      </template>
    </template>

    <template v-else>
      <p class="mt-2 text-[11px] leading-relaxed text-muted">
        {{ t("gateway.scope.listHint") }}
      </p>

      <!-- 批量控件：全选 / 全不选 / 反选 + 搜索 + 按能力批量并入 -->
      <div class="mt-2 flex flex-wrap items-center gap-1.5">
        <button
          type="button"
          class="rounded-[8px] border border-line px-2 py-1 text-[11.5px] text-ink-soft transition-colors hover:border-brand/50 hover:text-brand-dark"
          @click="selectAll"
        >
          {{ t("gateway.scope.selectAll") }}
        </button>
        <button
          type="button"
          class="rounded-[8px] border border-line px-2 py-1 text-[11.5px] text-ink-soft transition-colors hover:border-brand/50 hover:text-brand-dark"
          @click="selectNone"
        >
          {{ t("gateway.scope.selectNone") }}
        </button>
        <button
          type="button"
          class="inline-flex items-center gap-1 rounded-[8px] border border-line px-2 py-1 text-[11.5px] text-ink-soft transition-colors hover:border-brand/50 hover:text-brand-dark"
          @click="invert"
        >
          <Shuffle :size="12" :stroke-width="1.9" />
          {{ t("gateway.scope.invert") }}
        </button>

        <div class="relative ml-1">
          <Search
            :size="12"
            :stroke-width="1.9"
            class="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-muted"
          />
          <input
            v-model="search"
            type="text"
            :placeholder="t('gateway.scope.searchPlaceholder')"
            class="h-7 w-40 rounded-[8px] border border-line bg-surface pl-6 pr-2 text-[11.5px] text-ink placeholder:text-muted focus:border-[#d5d8d3] focus:outline-none"
          />
        </div>

        <span class="ml-auto text-[11px] text-muted">{{ t("gateway.scope.batchHint") }}</span>
        <button
          v-for="capability in CAPABILITIES"
          :key="capability"
          type="button"
          class="rounded-[8px] border border-line px-2 py-1 text-[11.5px] text-ink-soft transition-colors hover:border-brand/50 hover:text-brand-dark"
          @click="addByCapability(capability)"
        >
          + {{ t(`gateway.scope.cap.${capability}`) }}
        </button>
      </div>

      <div v-if="!props.catalog.length" class="mt-2 flex items-center gap-2 text-[11.5px] text-muted">
        <span>{{ t("gateway.keys.noCatalog") }}</span>
        <button
          type="button"
          class="rounded-[8px] border border-line px-2 py-1 text-[11.5px] text-ink-soft transition-colors hover:border-brand/50 hover:text-brand-dark"
          @click="emit('refresh')"
        >
          {{ t("gateway.models.refresh") }}
        </button>
      </div>
      <div v-else-if="!visible.length" class="mt-2 text-[11.5px] text-muted">
        {{ t("gateway.scope.searchEmpty") }}
      </div>
      <div v-else class="mt-2 flex flex-wrap gap-1.5">
        <button
          v-for="model in visible"
          :key="model.id"
          type="button"
          class="rounded-full border px-2.5 py-1 text-[11.5px] transition-colors"
          :class="
            props.models.includes(model.id)
              ? 'border-brand/40 bg-brand-soft text-brand-dark'
              : 'border-line text-ink-soft hover:border-[#d5d8d3] hover:text-ink'
          "
          @click="toggleModel(model.id)"
        >
          {{ model.id }}
        </button>
      </div>
      <button
        v-if="props.models.length"
        type="button"
        class="mt-2 text-[11.5px] text-muted transition-colors hover:text-ink"
        @click="selectNone"
      >
        {{ t("gateway.keys.clear") }}
      </button>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { Boxes } from "lucide-vue-next";
import EmptyState from "../../components/EmptyState.vue";
import SectionCard from "../../components/SectionCard.vue";
import SegmentedControl from "../../components/SegmentedControl.vue";
import {
  COMPONENT_CATEGORIES,
  componentCategoryKey,
  listComponents,
  type ComponentStatus,
} from "../../lib/componentRegistry";
import { t } from "../../lib/i18n";

/**
 * 设置 → 组件：前端通用组件的统一清单。
 *
 * 只读，数据来自 `lib/componentRegistry.ts`。新增组件的流程是
 * 「components/ 下建文件 + 注册表里登记一项」，缺了登记这里就看不到 ——
 * 这不是遗漏，而是故意的：清单必须能回答「项目里到底有哪些通用组件」。
 */
const keyword = ref("");
// 分段控件传的是普通字符串，不收紧联合类型，避免 v-model 类型冲突。
const category = ref("all");

const components = listComponents();

/** 只有非 stable 的状态才显示标签；空串同时作为「不显示」的判定。 */
function statusLabel(status?: ComponentStatus): string {
  return status && status !== "stable" ? t(`settings.components.status.${status}`) : "";
}

const categoryOptions = computed(() => [
  { value: "all", label: t("settings.components.category.all") },
  ...COMPONENT_CATEGORIES.map((item) => ({ value: item, label: t(componentCategoryKey(item)) })),
]);

const filtered = computed(() => {
  const query = keyword.value.trim().toLowerCase();
  return components.filter((item) => {
    if (category.value !== "all" && item.category !== category.value) return false;
    if (!query) return true;
    return (
      item.name.toLowerCase().includes(query) ||
      item.path.toLowerCase().includes(query) ||
      t(item.summaryKey).toLowerCase().includes(query)
    );
  });
});

const grouped = computed(() =>
  COMPONENT_CATEGORIES.map((item) => ({
    category: item,
    items: filtered.value.filter((entry) => entry.category === item),
  })).filter((group) => group.items.length > 0),
);
</script>

<template>
  <div class="space-y-3">
    <div class="flex flex-wrap items-center gap-2">
      <input
        v-model="keyword"
        type="text"
        :placeholder="t('settings.components.searchPlaceholder')"
        class="w-[240px] rounded-control border border-line bg-surface px-2.5 py-1.5 text-body-sm text-ink outline-none placeholder:text-muted focus:border-brand"
      />
      <SegmentedControl v-model="category" :options="categoryOptions" size="sm" />
      <span class="ml-auto text-caption text-muted">
        {{ t("settings.components.count", { count: filtered.length }) }}
      </span>
    </div>

    <SectionCard
      :title="t('settings.components.title')"
      :subtitle="t('settings.components.subtitle')"
      flush
    >
      <EmptyState
        v-if="!grouped.length"
        :icon="Boxes"
        :title="t('settings.components.empty')"
        :hint="t('settings.components.emptyHint')"
      />

      <div v-else>
        <section
          v-for="group in grouped"
          :key="group.category"
          class="border-b border-line-soft last:border-b-0"
        >
          <header
            class="flex items-center gap-2 bg-line-soft/40 px-5 py-2 text-caption font-medium text-ink-soft"
          >
            {{ t(componentCategoryKey(group.category)) }}
            <span class="text-caption font-normal text-muted">{{ group.items.length }}</span>
          </header>
          <ul class="divide-y divide-line-soft">
            <li v-for="item in group.items" :key="item.id" class="flex items-start gap-3 px-5 py-3">
              <div class="min-w-0 flex-1">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-body font-medium text-ink">{{ item.name }}</span>
                  <span
                    v-if="statusLabel(item.status)"
                    class="rounded-full bg-warn-soft px-2 py-0.5 text-micro font-semibold text-warn"
                  >
                    {{ statusLabel(item.status) }}
                  </span>
                </div>
                <p class="mt-0.5 text-body-sm leading-relaxed text-muted">
                  {{ t(item.summaryKey) }}
                </p>
              </div>
              <code class="shrink-0 font-mono text-micro text-muted">{{ item.path }}</code>
            </li>
          </ul>
        </section>
      </div>
    </SectionCard>

    <p class="text-caption leading-relaxed text-muted">{{ t("settings.components.source") }}</p>
  </div>
</template>

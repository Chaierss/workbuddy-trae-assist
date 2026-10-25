<script setup lang="ts">
import { computed, ref } from "vue";
import { ListChecks } from "lucide-vue-next";
import EmptyState from "../../components/EmptyState.vue";
import SectionCard from "../../components/SectionCard.vue";
import SegmentedControl from "../../components/SegmentedControl.vue";
import { t } from "../../lib/i18n";
import {
  PROJECT_RULES,
  RULE_CATEGORIES,
  ruleCategoryKey,
} from "../../lib/rules";

/**
 * 设置 → 规则：只读展示项目硬性约束与工程约定。
 *
 * 数据来自 `lib/rules.ts`，本页不做增删改 —— 规则要变就改代码走评审。
 * 筛选只做「类别 + 关键字」，与日志页保持同一套交互习惯。
 */
const keyword = ref("");
// 分段控件的取值是普通字符串，这里不收紧成联合类型，避免 v-model 类型冲突。
const category = ref("all");

const categoryOptions = computed(() => [
  { value: "all", label: t("settings.rules.category.all") },
  ...RULE_CATEGORIES.map((item) => ({ value: item, label: t(ruleCategoryKey(item)) })),
]);

const filtered = computed(() => {
  const query = keyword.value.trim().toLowerCase();
  return PROJECT_RULES.filter((rule) => {
    if (category.value !== "all" && rule.category !== category.value) return false;
    if (!query) return true;
    return (
      t(rule.titleKey).toLowerCase().includes(query) ||
      t(rule.detailKey).toLowerCase().includes(query)
    );
  });
});

/** 分组顺序跟随 `RULE_CATEGORIES`，空组直接不渲染。 */
const grouped = computed(() =>
  RULE_CATEGORIES.map((item) => ({
    category: item,
    rules: filtered.value.filter((rule) => rule.category === item),
  })).filter((group) => group.rules.length > 0),
);

const levelTone: Record<string, string> = {
  mandatory: "bg-danger-soft text-danger",
  convention: "bg-info-soft text-info",
};
</script>

<template>
  <div class="space-y-3">
    <div class="flex flex-wrap items-center gap-2">
      <input
        v-model="keyword"
        type="text"
        :placeholder="t('settings.rules.searchPlaceholder')"
        class="w-[240px] rounded-control border border-line bg-surface px-2.5 py-1.5 text-body-sm text-ink outline-none placeholder:text-muted focus:border-brand"
      />
      <SegmentedControl v-model="category" :options="categoryOptions" size="sm" />
      <span class="ml-auto text-caption text-muted">
        {{ t("settings.rules.count", { count: filtered.length }) }}
      </span>
    </div>

    <SectionCard :title="t('settings.rules.title')" :subtitle="t('settings.rules.subtitle')" flush>
      <EmptyState
        v-if="!grouped.length"
        :icon="ListChecks"
        :title="t('settings.rules.empty')"
        :hint="t('settings.rules.emptyHint')"
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
            {{ t(ruleCategoryKey(group.category)) }}
            <span class="text-caption font-normal text-muted">{{ group.rules.length }}</span>
          </header>
          <ul class="divide-y divide-line-soft">
            <li v-for="rule in group.rules" :key="rule.id" class="px-5 py-3">
              <div class="flex flex-wrap items-center gap-2">
                <span
                  class="shrink-0 rounded-full px-2 py-0.5 text-micro font-semibold"
                  :class="levelTone[rule.level]"
                >
                  {{ t(`settings.rules.level.${rule.level}`) }}
                </span>
                <span class="text-body font-medium text-ink">{{ t(rule.titleKey) }}</span>
                <span class="ml-auto font-mono text-micro text-muted">{{ rule.id }}</span>
              </div>
              <p class="mt-1 text-body-sm leading-relaxed text-ink-soft">{{ t(rule.detailKey) }}</p>
            </li>
          </ul>
        </section>
      </div>
    </SectionCard>

    <p class="text-caption leading-relaxed text-muted">{{ t("settings.rules.source") }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { Check, Copy, ListChecks } from "lucide-vue-next";
import EmptyState from "../../components/EmptyState.vue";
import SectionCard from "../../components/SectionCard.vue";
import SegmentedControl from "../../components/SegmentedControl.vue";
import { t } from "../../lib/i18n";
import { isDev } from "../../lib/devMode";
import {
  PAGE_MODES,
  PROJECT_RULES,
  RULE_CATEGORIES,
  ruleCategoryKey,
  ruleLevelKey,
  rulesMarkdown,
} from "../../lib/rules";

/**
 * 设置 → 规则：只读展示项目硬性约束与工程约定。
 *
 * 数据来自 `keel/rules.json`，本页不做增删改 —— 规则要变就改 JSON 走评审。
 * 术语表与规则出处属于内部事实，只在开发模式出现；导出的 Markdown 给 AI 当上下文。
 */
const keyword = ref("");
// 分段控件的取值是普通字符串，这里不收紧成联合类型，避免 v-model 类型冲突。
const category = ref("all");

const dev = isDev();
const copied = ref(false);

async function exportForAi(): Promise<void> {
  try {
    await navigator.clipboard.writeText(rulesMarkdown());
    copied.value = true;
    window.setTimeout(() => {
      copied.value = false;
    }, 2000);
  } catch {
    window.alert(t("settings.rules.exportFailed"));
  }
}

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
      <div class="ml-auto flex items-center gap-2">
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-control border border-line px-2 py-1 text-caption text-ink-soft transition-colors hover:border-line-strong hover:text-ink"
          @click="exportForAi"
        >
          <Check v-if="copied" :size="12" />
          <Copy v-else :size="12" />
          {{ copied ? t("settings.rules.exportDone") : t("settings.rules.export") }}
        </button>
        <span class="text-caption text-muted">
          {{ t("settings.rules.count", { count: filtered.length }) }}
        </span>
      </div>
    </div>

    <!-- 术语表只给内部看：线上形态隐藏「专注模式」这类设计语言 -->
    <SectionCard
      v-if="dev"
      :title="t('settings.rules.glossary')"
      :subtitle="t('settings.rules.glossaryHint')"
      flush
    >
      <ul class="divide-y divide-line-soft">
        <li v-for="mode in PAGE_MODES" :key="mode.key" class="px-5 py-3">
          <div class="flex flex-wrap items-baseline gap-2">
            <span class="text-body font-medium text-ink">{{ mode.name }}</span>
            <span class="font-mono text-micro text-muted">{{ mode.english }}</span>
          </div>
          <p class="mt-1 text-body-sm leading-relaxed text-ink-soft">{{ mode.definition }}</p>
          <p class="mt-1 text-caption leading-relaxed text-muted">
            {{ t("settings.rules.signal") }} · {{ mode.signal }}
          </p>
          <p class="mt-0.5 text-caption leading-relaxed text-muted">
            {{ t("settings.rules.avoid") }} · {{ mode.avoid.join("；") }}
          </p>
        </li>
      </ul>
    </SectionCard>

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
                  {{ t(ruleLevelKey(rule.level)) }}
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

    <p v-if="dev" class="text-caption leading-relaxed text-muted">{{ t("settings.rules.source") }}</p>
  </div>
</template>

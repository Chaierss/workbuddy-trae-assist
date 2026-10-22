<script setup lang="ts">
import { computed } from "vue";
import { Cable, RefreshCw } from "lucide-vue-next";
import SectionCard from "../../components/SectionCard.vue";
import SelectMenu from "../../components/SelectMenu.vue";
import StatusPill from "../../components/StatusPill.vue";
import { formatTime } from "../../api";
import { t } from "../../lib/i18n";
import { compact, useGateway } from "./useGateway";

/** 模型目录：以上游 CLI 白名单为准，标注上下文 / 最大输出 / 计费。 */
const store = useGateway();

/** 可选的自动刷新间隔（分钟）；0 = 关闭，单独一项。 */
const refreshChoices = [5, 15, 30, 60];
const refreshOptions = computed(() => [
  { value: 0, label: t("gateway.models.autoRefreshOff") },
  ...refreshChoices.map((minutes) => ({
    value: minutes,
    label: t("gateway.models.autoRefreshMinutes", { minutes }),
  })),
]);

function creditsLabel(credits: string): string {
  if (!credits) return "";
  // 上游串形如 `x0.00` / `x0.5`；`x0.00` 即限时免费。
  const match = credits.match(/x([\d.]+)$/);
  if (!match) return credits;
  const value = Number(match[1]);
  if (!Number.isFinite(value)) return credits;
  return value === 0 ? t("gateway.models.free") : t("gateway.models.creditMultiplier", { value });
}

/** 改间隔立即写配置（后端按下开关即生效），无需再点保存。 */
function setRefreshMinutes(value: number): void {
  void store.save({ catalogRefreshMinutes: value });
}
</script>

<template>
  <div v-if="!store.status" class="py-8 text-center text-[12.5px] text-muted">
    {{ t("gateway.loading") }}
  </div>

  <SectionCard v-else :title="t('gateway.models.title')" :subtitle="t('gateway.models.subtitle')">
    <template #actions>
      <StatusPill :tone="store.status.catalog.fresh ? 'neutral' : 'warn'">
        {{
          store.status.catalog.fresh ? t("gateway.models.cached") : t("gateway.models.notFetched")
        }}
      </StatusPill>
      <span class="text-[11.5px] text-muted">
        {{
          store.status.catalog.fetchedAt
            ? t("gateway.models.fetchedAt", { time: formatTime(store.status.catalog.fetchedAt) })
            : t("gateway.models.neverFetched")
        }}
      </span>
      <label class="flex items-center gap-1.5 text-[11.5px] text-muted">
        {{ t("gateway.models.autoRefresh") }}
        <SelectMenu
          :model-value="store.status.catalogRefreshMinutes"
          :options="refreshOptions"
          size="sm"
          :disabled="store.saving"
          class="w-[104px]"
          @update:model-value="setRefreshMinutes"
        />
      </label>
      <button
        type="button"
        class="inline-flex items-center gap-1.5 rounded-[9px] border border-line px-2.5 py-2 text-[12px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink disabled:opacity-60"
        :disabled="store.loading"
        @click="store.refreshModels"
      >
        <RefreshCw :size="13" :stroke-width="1.9" :class="store.loading ? 'animate-spin' : ''" />
        {{ t("gateway.models.refresh") }}
      </button>
    </template>

    <div v-if="!store.status.catalog.models.length" class="py-6 text-center text-[12.5px] text-muted">
      {{ t("gateway.models.empty") }}
    </div>

    <div v-else class="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
      <div
        v-for="model in store.status.catalog.models"
        :key="model.id"
        class="rounded-[10px] border border-line-soft px-3.5 py-2.5"
      >
        <div class="flex items-center gap-2">
          <Cable :size="14" :stroke-width="1.9" class="shrink-0 text-muted" />
          <span class="truncate font-mono text-[12px] text-ink" :title="model.id">{{ model.id }}</span>
          <StatusPill v-if="model.free" tone="brand">{{ t("gateway.models.freeBadge") }}</StatusPill>
        </div>
        <div class="mt-0.5 truncate text-[11.5px] text-muted">{{ model.name }}</div>

        <!-- 能力标注：上下文 / 最大输出 / 计费 / 图片 -->
        <div class="mt-2 flex flex-wrap gap-1.5 text-[10.5px]">
          <span class="rounded-md bg-line-soft px-1.5 py-0.5 text-ink-soft">
            {{ t("gateway.models.context", { value: compact(model.contextWindow) }) }}
          </span>
          <span
            v-if="model.maxOutputTokens"
            class="rounded-md bg-line-soft px-1.5 py-0.5 text-ink-soft"
          >
            {{ t("gateway.models.output", { value: compact(model.maxOutputTokens) }) }}
          </span>
          <span
            v-if="creditsLabel(model.credits)"
            class="rounded-md px-1.5 py-0.5"
            :class="model.free ? 'bg-brand-soft text-brand-dark' : 'bg-line-soft text-ink-soft'"
          >
            {{ creditsLabel(model.credits) }}
          </span>
          <span v-if="model.supportsImages" class="rounded-md bg-line-soft px-1.5 py-0.5 text-ink-soft">
            {{ t("gateway.models.supportsImages") }}
          </span>
          <span
            v-for="badge in model.badges"
            :key="badge"
            class="rounded-md bg-line-soft px-1.5 py-0.5 text-ink-soft"
          >
            {{ badge }}
          </span>
        </div>
      </div>
    </div>
  </SectionCard>
</template>

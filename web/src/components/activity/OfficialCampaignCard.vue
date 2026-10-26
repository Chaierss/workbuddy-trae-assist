<script setup lang="ts">
/** 一张官方活动卡片：主题色 + 图标 + 状态 + 进度 + 两个动作。 */
import { computed } from "vue";
import {
  CalendarCheck,
  ExternalLink,
  Flame,
  GraduationCap,
  Loader2,
  Moon,
  Plane,
  Sprout,
  Sun,
  Wand2,
  type LucideIcon,
} from "lucide-vue-next";
import StatusPill from "../StatusPill.vue";
import CampaignProgress from "./CampaignProgress.vue";
import { CAMPAIGN_STATUS, CAMPAIGN_THEME, safeActivityUrl } from "../../lib/activity";
import { t } from "../../lib/i18n";
import type { ActivityCampaign, CampaignIcon } from "../../api";

const props = defineProps<{
  campaign: ActivityCampaign;
  running?: boolean;
}>();

const emit = defineEmits<{
  open: [];
  run: [];
}>();

const ICONS: Record<CampaignIcon, LucideIcon> = {
  "calendar-check": CalendarCheck,
  sprout: Sprout,
  flame: Flame,
  plane: Plane,
  moon: Moon,
  "graduation-cap": GraduationCap,
  sun: Sun,
};

const icon = computed(() => ICONS[props.campaign.icon] ?? CalendarCheck);
const theme = computed(() => CAMPAIGN_THEME[props.campaign.theme] ?? CAMPAIGN_THEME.green);
const status = computed(() => CAMPAIGN_STATUS[props.campaign.status] ?? CAMPAIGN_STATUS.not_detected);
const officialUrl = computed(() => safeActivityUrl(props.campaign.officialUrl));
const canRun = computed(() => props.campaign.actionable > 0 && !props.running);

function openDetail(): void {
  emit("open");
}

function run(): void {
  if (!canRun.value) return;
  emit("run");
}
</script>

<template>
  <article
    class="group relative flex cursor-pointer flex-col overflow-hidden rounded-card border border-line bg-surface shadow-card transition-all"
    :class="[theme.hover, 'hover:-translate-y-0.5 hover:shadow-lg']"
    role="button"
    tabindex="0"
    :aria-label="`${campaign.title} · ${t(status.labelKey)}`"
    @click="openDetail"
    @keydown.enter.prevent="openDetail"
    @keydown.space.prevent="openDetail"
  >
    <span class="absolute inset-x-0 top-0 h-[3px]" :class="theme.bar" />

    <div class="flex items-start gap-3 px-4 pt-4">
      <span class="grid size-9 shrink-0 place-items-center rounded-control" :class="theme.icon">
        <component :is="icon" :size="17" :stroke-width="1.9" />
      </span>
      <div class="min-w-0 flex-1">
        <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
          <h3 class="text-body font-medium text-ink">{{ campaign.title }}</h3>
          <StatusPill v-if="campaign.featured" tone="brand">
            {{ t("activity.officialFeatured") }}
          </StatusPill>
          <StatusPill :tone="status.tone">{{ t(status.labelKey) }}</StatusPill>
        </div>
        <p class="mt-0.5 truncate text-caption text-muted" :title="campaign.subtitle">
          {{ campaign.subtitle }}
        </p>
      </div>
    </div>

    <div class="mt-3 flex flex-wrap items-center gap-1 px-4">
      <span
        v-for="tag in campaign.tags.slice(0, 3)"
        :key="tag"
        class="rounded-full border border-line px-1.5 py-[1px] text-micro leading-tight text-muted"
      >
        {{ tag }}
      </span>
      <span class="text-micro text-muted">{{ campaign.periodNote }}</span>
    </div>

    <div class="mt-3 px-4">
      <CampaignProgress :campaign="campaign" />
    </div>

    <div class="mt-auto flex items-center gap-2 px-4 pt-3 pb-4">
      <button
        type="button"
        class="inline-flex items-center gap-1.5 rounded-control border border-line px-2.5 py-1.5 text-caption transition-colors hover:border-line-strong hover:text-ink disabled:opacity-45"
        :class="canRun ? 'text-ink-soft' : 'text-muted'"
        :disabled="!canRun"
        :title="
          campaign.actionable
            ? t('activity.officialRunHint', { title: campaign.title })
            : t('activity.officialNothingToRun')
        "
        @click.stop="run"
      >
        <Loader2 v-if="running" :size="12" class="animate-spin" />
        <Wand2 v-else :size="12" :stroke-width="1.9" />
        {{
          running
            ? t("activity.running")
            : campaign.actionable
              ? t("activity.officialRunCount", { count: campaign.actionable })
              : t("activity.officialRun")
        }}
      </button>

      <span class="ml-auto text-caption text-muted transition-colors group-hover:text-ink">
        {{ t("activity.officialViewDetail") }}
      </span>
      <a
        v-if="officialUrl"
        :href="officialUrl"
        target="_blank"
        rel="noopener noreferrer"
        class="shrink-0 text-muted transition-colors hover:text-brand"
        :title="campaign.officialLabel"
        @click.stop
      >
        <ExternalLink :size="13" :stroke-width="1.9" />
      </a>
    </div>
  </article>
</template>

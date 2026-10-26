<script setup lang="ts">
/**
 * 概览页「官方活动速览」。
 *
 * 只放 3 个活动：主推 / 可执行最多的排前面。点击深链到活动页官方活动标签，
 * 并自动打开对应活动的详情抽屉。
 */
import { computed } from "vue";
import { RouterLink } from "vue-router";
import {
  ArrowRight,
  CalendarCheck,
  Flame,
  GraduationCap,
  Moon,
  Plane,
  Sprout,
  Sun,
  type LucideIcon,
} from "lucide-vue-next";
import SectionCard from "./SectionCard.vue";
import StatusPill from "./StatusPill.vue";
import { CAMPAIGN_STATUS, CAMPAIGN_THEME, pickHighlights } from "../lib/activity";
import { t } from "../lib/i18n";
import { scopedPath } from "../stores/app";
import type { ActivityCampaign, CampaignIcon } from "../api";

const props = defineProps<{ campaigns: ActivityCampaign[] }>();

const ICONS: Record<CampaignIcon, LucideIcon> = {
  "calendar-check": CalendarCheck,
  sprout: Sprout,
  flame: Flame,
  plane: Plane,
  moon: Moon,
  "graduation-cap": GraduationCap,
  sun: Sun,
};

const highlights = computed(() => pickHighlights(props.campaigns, 3));
/** 概览只报「有多少账号此刻真的可以参与」，不重复列每个活动的口径。 */
const actionableAccounts = computed(() =>
  props.campaigns.reduce((total, item) => total + item.actionable, 0),
);

function campaignLink(key: string): string {
  return scopedPath(`/activity?tab=official&campaign=${encodeURIComponent(key)}`);
}
</script>

<template>
  <SectionCard
    :title="t('overview.official.title')"
    :subtitle="t('overview.official.subtitle')"
  >
    <template #actions>
      <RouterLink
        :to="scopedPath('/activity?tab=official')"
        class="inline-flex items-center gap-0.5 text-body-sm text-muted transition-colors hover:text-ink"
      >
        {{ t("overview.official.viewAll") }}
        <ArrowRight :size="12" />
      </RouterLink>
    </template>

    <div v-if="!highlights.length" class="py-6 text-center text-body-sm text-muted">
      {{ t("overview.official.empty") }}
    </div>

    <p v-else-if="actionableAccounts" class="mb-2.5 text-caption text-warn">
      {{ t("overview.official.accountsHint", { count: actionableAccounts }) }}
    </p>

    <div v-else class="grid gap-3 sm:grid-cols-3">
      <RouterLink
        v-for="campaign in highlights"
        :key="campaign.key"
        :to="campaignLink(campaign.key)"
        class="group relative overflow-hidden rounded-control border border-line bg-surface px-3.5 py-3 transition-all hover:-translate-y-0.5 hover:shadow-md"
        :class="CAMPAIGN_THEME[campaign.theme].hover"
      >
        <span class="absolute inset-y-0 left-0 w-[3px]" :class="CAMPAIGN_THEME[campaign.theme].bar" />
        <div class="flex items-start gap-2">
          <span
            class="grid size-8 shrink-0 place-items-center rounded-control"
            :class="CAMPAIGN_THEME[campaign.theme].icon"
          >
            <component :is="ICONS[campaign.icon] ?? CalendarCheck" :size="15" :stroke-width="1.9" />
          </span>
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-1.5">
              <span class="truncate text-body-sm font-medium text-ink">{{ campaign.title }}</span>
              <StatusPill :tone="CAMPAIGN_STATUS[campaign.status].tone">
                {{ t(CAMPAIGN_STATUS[campaign.status].labelKey) }}
              </StatusPill>
            </div>
            <p class="mt-0.5 truncate text-caption text-muted">{{ campaign.subtitle }}</p>
          </div>
        </div>

        <div class="mt-2.5 flex items-baseline justify-between gap-2 text-caption">
          <span class="text-muted">
            {{ t("overview.official.done", { done: campaign.done, total: campaign.accountsTotal }) }}
          </span>
          <span v-if="campaign.actionable" class="font-medium text-warn">
            {{ t("overview.official.actionable", { count: campaign.actionable }) }}
          </span>
          <span v-else class="text-brand-dark">{{ t("overview.official.allDone") }}</span>
        </div>
        <div class="mt-1 h-1 overflow-hidden rounded-full bg-line-soft">
          <div
            class="h-full rounded-full transition-[width] duration-500"
            :class="CAMPAIGN_THEME[campaign.theme].bar"
            :style="{
              width: `${campaign.accountsTotal ? Math.round((campaign.done / campaign.accountsTotal) * 100) : 0}%`,
            }"
          />
        </div>
      </RouterLink>
    </div>
  </SectionCard>
</template>

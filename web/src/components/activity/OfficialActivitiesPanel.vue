<script setup lang="ts">
/**
 * 官方活动面板。
 *
 * 信息层级：主推活动（Hero）→ 其余活动卡片画廊 → 免责声明。
 * 卡片只做「活动级」展示；具体到账号的进度放在详情抽屉里，避免首屏铺 16 个账号。
 */
import { computed, ref, watch } from "vue";
import {
  CalendarCheck,
  Flame,
  GraduationCap,
  Loader2,
  Moon,
  Plane,
  RefreshCw,
  Sparkles,
  Sprout,
  Sun,
  Wand2,
  type LucideIcon,
} from "lucide-vue-next";
import SectionCard from "../SectionCard.vue";
import StatusPill from "../StatusPill.vue";
import OfficialCampaignCard from "./OfficialCampaignCard.vue";
import OfficialCampaignDrawer from "./OfficialCampaignDrawer.vue";
import CampaignProgress from "./CampaignProgress.vue";
import {
  CAMPAIGN_STATUS,
  CAMPAIGN_THEME,
  campaignPeriod,
  pickHighlights,
  safeActivityUrl,
  sortCampaigns,
} from "../../lib/activity";
import { t } from "../../lib/i18n";
import type {
  AccountActivities,
  ActivityCampaign,
  ActivityCatalog,
  ActivityRun,
  CampaignIcon,
} from "../../api";

const ICONS: Record<CampaignIcon, LucideIcon> = {
  "calendar-check": CalendarCheck,
  sprout: Sprout,
  flame: Flame,
  plane: Plane,
  moon: Moon,
  "graduation-cap": GraduationCap,
  sun: Sun,
};

const props = withDefaults(
  defineProps<{
    campaigns: ActivityCampaign[];
    accounts: AccountActivities[];
    catalog?: ActivityCatalog | null;
    detectedAt?: string | null;
    runs?: ActivityRun[];
    busy?: boolean;
    running?: boolean;
    runningKey?: string | null;
    initialCampaign?: string | null;
  }>(),
  {
    catalog: null,
    detectedAt: null,
    runs: () => [],
    busy: false,
    running: false,
    runningKey: null,
    initialCampaign: null,
  },
);

const emit = defineEmits<{
  run: [accountIds: string[], keys: string[]];
  refresh: [];
  select: [key: string | null];
}>();

const platformFilter = ref<"all" | "workbuddy" | "trae">("all");
const groupFilter = ref<"all" | "daily" | "seasonal">("all");
const hideDone = ref(false);
const selectedKey = ref<string | null>(props.initialCampaign);

watch(
  () => props.initialCampaign,
  (key) => {
    if (key) selectedKey.value = key;
  },
);

const selected = computed(
  () => props.campaigns.find((item) => item.key === selectedKey.value) ?? null,
);

const filtered = computed(() =>
  sortCampaigns(
    props.campaigns.filter((item) => {
      if (platformFilter.value !== "all" && item.platform !== platformFilter.value) return false;
      if (groupFilter.value !== "all" && item.group !== groupFilter.value) return false;
      if (hideDone.value && item.allDone) return false;
      return true;
    }),
  ),
);

const hero = computed(() => {
  if (platformFilter.value !== "all" || groupFilter.value !== "all") return null;
  return pickHighlights(props.campaigns, 1)[0] ?? null;
});

const grid = computed(() =>
  hero.value ? filtered.value.filter((item) => item.key !== hero.value?.key) : filtered.value,
);

const heroTheme = computed(() =>
  hero.value ? CAMPAIGN_THEME[hero.value.theme] : CAMPAIGN_THEME.green,
);
const heroStatus = computed(() =>
  hero.value ? CAMPAIGN_STATUS[hero.value.status] : CAMPAIGN_STATUS.no_accounts,
);
const heroIcon = computed(() => ICONS[hero.value?.icon ?? "calendar-check"] ?? CalendarCheck);
const heroUrl = computed(() => safeActivityUrl(hero.value?.officialUrl));
const heroPeriod = computed(() =>
  hero.value ? campaignPeriod(hero.value.periodStart, hero.value.periodEnd) : "",
);

const empty = computed(() => !props.campaigns.length);

function openCampaign(key: string): void {
  selectedKey.value = key;
  emit("select", key);
}

function closeCampaign(): void {
  selectedKey.value = null;
  emit("select", null);
}

function runCampaign(campaign: ActivityCampaign): void {
  emit("run", [], [campaign.key]);
}
</script>

<template>
  <div>
    <div class="flex flex-wrap items-center gap-x-4 gap-y-2">
      <div class="min-w-0 flex-1">
        <p class="text-body-sm text-muted">{{ t("activity.officialSubtitle") }}</p>
      </div>
      <div class="flex flex-wrap items-center gap-2 text-caption text-muted">
        <span v-if="catalog" class="rounded-full border border-line px-2 py-0.5">
          {{ t("activity.officialCatalogVersion", { version: catalog.version }) }}
        </span>
        <span v-if="catalog" class="rounded-full border border-line px-2 py-0.5">
          {{ t("activity.officialCatalogUpdated", { date: catalog.updatedAt }) }}
        </span>
        <span v-if="detectedAt">
          {{ t("activity.officialDetectedAt", { time: detectedAt.slice(11, 16) }) }}
        </span>
        <span v-if="busy" class="inline-flex items-center gap-1">
          <RefreshCw :size="11" class="animate-spin" />
          {{ t("activity.detectingSuffix") }}
        </span>
      </div>
    </div>

    <div class="mt-3 flex flex-wrap items-center gap-3">
      <div class="flex items-center gap-1 rounded-control border border-line bg-surface p-1">
        <button
          v-for="option in [
            { key: 'all', label: t('activity.filterAll') },
            { key: 'workbuddy', label: 'WorkBuddy' },
            { key: 'trae', label: 'Trae' },
          ]"
          :key="option.key"
          type="button"
          class="rounded-control px-2.5 py-1 text-body-sm transition-colors"
          :class="
            platformFilter === option.key ? 'bg-line-soft text-ink' : 'text-muted hover:text-ink-soft'
          "
          @click="platformFilter = option.key as 'all' | 'workbuddy' | 'trae'"
        >
          {{ option.label }}
        </button>
      </div>
      <div class="flex items-center gap-1 rounded-control border border-line bg-surface p-1">
        <button
          v-for="option in [
            { key: 'all', label: t('activity.officialFilterAll') },
            { key: 'daily', label: t('activity.groupDaily') },
            { key: 'seasonal', label: t('activity.groupSeasonal') },
          ]"
          :key="option.key"
          type="button"
          class="rounded-control px-2.5 py-1 text-body-sm transition-colors"
          :class="
            groupFilter === option.key ? 'bg-line-soft text-ink' : 'text-muted hover:text-ink-soft'
          "
          @click="groupFilter = option.key as 'all' | 'daily' | 'seasonal'"
        >
          {{ option.label }}
        </button>
      </div>
      <label class="inline-flex cursor-pointer items-center gap-1.5 text-body-sm text-muted">
        <input v-model="hideDone" type="checkbox" class="size-3.5 accent-brand" />
        {{ t("activity.filterHideDone") }}
      </label>
    </div>

    <SectionCard v-if="empty" class="mt-4">
      <div class="py-10 text-center text-body text-muted">{{ t("activity.officialEmpty") }}</div>
    </SectionCard>

    <template v-else>
      <!-- 主推活动：一张通栏 Hero，先把「最值得做的事」说清楚 -->
      <article
        v-if="hero"
        class="relative mt-4 overflow-hidden rounded-card border border-line bg-surface shadow-card"
      >
        <span class="absolute inset-y-0 left-0 w-[4px]" :class="heroTheme.bar" />
        <div class="flex flex-wrap items-start gap-4 px-5 py-4">
          <span
            class="grid size-12 shrink-0 place-items-center rounded-card"
            :class="heroTheme.icon"
          >
            <component :is="heroIcon" :size="22" :stroke-width="1.8" />
          </span>
          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-center gap-2">
              <span class="inline-flex items-center gap-1 rounded-full bg-brand-soft px-2 py-0.5 text-caption font-medium text-brand-dark">
                <Sparkles :size="11" />
                {{ t("activity.officialFeatured") }}
              </span>
              <StatusPill :tone="heroStatus.tone">{{ t(heroStatus.labelKey) }}</StatusPill>
              <span class="text-caption text-muted">{{ hero.periodNote }}</span>
              <span v-if="heroPeriod" class="text-caption text-muted">{{ heroPeriod }}</span>
            </div>
            <h2 class="mt-1.5 text-heading font-semibold text-ink">{{ hero.title }}</h2>
            <p class="mt-1 max-w-[720px] text-body-sm leading-relaxed text-muted">
              {{ hero.description }}
            </p>
            <div class="mt-2 flex flex-wrap items-center gap-1">
              <span
                v-for="tag in hero.tags"
                :key="tag"
                class="rounded-full border border-line px-2 py-0.5 text-micro text-muted"
              >
                {{ tag }}
              </span>
            </div>
          </div>
          <div class="w-full max-w-[320px] shrink-0">
            <CampaignProgress :campaign="hero" />
            <div class="mt-3 flex items-center gap-2">
              <button
                type="button"
                class="inline-flex flex-1 items-center justify-center gap-1.5 rounded-control bg-brand px-3 py-2 text-body-sm font-medium text-white transition-colors hover:bg-brand-dark disabled:opacity-40"
                :disabled="!hero.actionable || running"
                @click="runCampaign(hero)"
              >
                <Loader2 v-if="running && runningKey === hero.key" :size="14" class="animate-spin" />
                <Wand2 v-else :size="14" :stroke-width="1.9" />
                {{ t("activity.officialRunCount", { count: hero.actionable }) }}
              </button>
              <button
                type="button"
                class="rounded-control border border-line px-3 py-2 text-body-sm text-ink-soft transition-colors hover:border-line-strong hover:text-ink"
                @click="openCampaign(hero.key)"
              >
                {{ t("activity.officialViewDetail") }}
              </button>
            </div>
            <a
              v-if="heroUrl"
              :href="heroUrl"
              target="_blank"
              rel="noreferrer"
              class="mt-2 block text-center text-caption text-muted hover:text-brand"
            >
              {{ hero.officialLabel }}
            </a>
          </div>
        </div>
      </article>

      <div v-if="grid.length" class="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        <OfficialCampaignCard
          v-for="campaign in grid"
          :key="campaign.key"
          :campaign="campaign"
          :running="running && runningKey === campaign.key"
          @open="openCampaign(campaign.key)"
          @run="runCampaign(campaign)"
        />
      </div>

      <SectionCard v-else-if="!hero" class="mt-4">
        <div class="py-8 text-center text-body text-muted">
          {{ t("activity.officialNoMatch") }}
        </div>
      </SectionCard>

      <p class="mt-4 text-caption leading-relaxed text-muted">
        {{ catalog?.disclaimer || t("activity.officialDisclaimer") }}
      </p>
    </template>

    <OfficialCampaignDrawer
      :campaign="selected"
      :accounts="accounts"
      :runs="runs"
      :running="running && runningKey === selected?.key"
      @close="closeCampaign"
      @run="(accountIds, keys) => emit('run', accountIds, keys)"
    />
  </div>
</template>

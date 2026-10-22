<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { AlertCircle, ArrowUpCircle, CheckCircle2, Download, Loader2, RefreshCw } from "lucide-vue-next";
import SectionCard from "../../components/SectionCard.vue";
import { t } from "../../lib/i18n";
import { desktop, type DesktopUpdateState } from "../../lib/desktop";

/**
 * 「更新」分栏（仅桌面端）。
 *
 * 数据来自主进程的更新状态机：这里不自己判断有没有新版本，只负责把
 * `update:state` 画出来。组件卸载时要退订，否则来回切 tab 会累积监听。
 */
const state = ref<DesktopUpdateState | null>(null);
const busy = ref(false);
let unsubscribe: (() => void) | null = null;

const updating = computed(() => {
  const status = state.value?.status;
  return status === "checking" || status === "downloading";
});

const platformLabel = computed(() => {
  const platform = desktop?.platform;
  if (platform === "win32") return "Windows";
  if (platform === "darwin") return "macOS";
  if (platform === "linux") return "Linux";
  return platform ?? "—";
});

const statusText = computed(() => {
  const current = state.value;
  if (!current) return t("settings.update.status.idle");
  switch (current.status) {
    case "disabled":
      return current.message || t("settings.update.status.disabled");
    case "checking":
      return t("settings.update.status.checking");
    case "up-to-date":
      return t("settings.update.status.upToDate", { version: current.currentVersion });
    case "available":
      return t("settings.update.status.available", { version: current.version ?? "" });
    case "downloading":
      return t("settings.update.status.downloading", { percent: String(current.percent) });
    case "downloaded":
      return t("settings.update.status.downloaded", { version: current.version ?? "" });
    case "error":
      return current.message || t("settings.update.status.error");
    default:
      return t("settings.update.status.idle");
  }
});

/** 「最近检查」时间用相对时间，比一串 ISO 串好读。 */
const checkedText = computed(() => {
  const raw = state.value?.checkedAt;
  if (!raw) return "";
  const diff = Date.now() - new Date(raw).getTime();
  if (Number.isNaN(diff) || diff < 0) return "";
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return t("settings.update.checkedJustNow");
  if (minutes < 60) return t("settings.update.checkedMinutes", { count: String(minutes) });
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return t("settings.update.checkedHours", { count: String(hours) });
  return t("settings.update.checkedDays", { count: String(Math.floor(hours / 24)) });
});

async function check(): Promise<void> {
  if (!desktop || busy.value) return;
  busy.value = true;
  try {
    state.value = await desktop.update.check();
  } finally {
    busy.value = false;
  }
}

async function download(): Promise<void> {
  if (!desktop) return;
  state.value = await desktop.update.download();
}

async function install(): Promise<void> {
  await desktop?.update.install();
}

onMounted(async () => {
  if (!desktop) return;
  state.value = await desktop.update.get();
  unsubscribe = desktop.update.onState((next) => {
    state.value = next;
  });
});

onUnmounted(() => {
  unsubscribe?.();
});
</script>

<template>
  <div class="space-y-4">
    <SectionCard :title="t('settings.update.title')" :subtitle="t('settings.update.subtitle')">
      <div class="flex items-start justify-between gap-4">
        <div class="min-w-0">
          <div class="text-[13px] font-medium text-ink">
            chaierww {{ state?.currentVersion ?? desktop?.version ?? "" }}
          </div>
          <div class="mt-1 flex items-center gap-1.5 text-[12px] text-muted">
            <Loader2 v-if="updating" :size="13" class="shrink-0 animate-spin" />
            <CheckCircle2
              v-else-if="state?.status === 'up-to-date'"
              :size="13"
              class="shrink-0 text-brand"
            />
            <AlertCircle
              v-else-if="state?.status === 'error'"
              :size="13"
              class="shrink-0 text-[var(--color-danger,#dc2626)]"
            />
            <ArrowUpCircle
              v-else-if="state?.status === 'available' || state?.status === 'downloaded'"
              :size="13"
              class="shrink-0 text-brand"
            />
            <span class="min-w-0">{{ statusText }}</span>
          </div>
          <div v-if="checkedText" class="mt-1 text-[11.5px] text-muted">{{ checkedText }}</div>
        </div>

        <button
          type="button"
          class="shrink-0 rounded-[9px] border border-line px-3 py-1.5 text-[12.5px] text-ink transition-colors hover:bg-line-soft disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="busy || updating"
          @click="check"
        >
          <RefreshCw :size="13" class="mr-1 inline-block align-[-2px]" :class="busy || updating ? 'animate-spin' : ''" />
          {{ t("settings.update.check") }}
        </button>
      </div>

      <!-- 有新版本 / 正在下载 / 已下载：一条状态条说清楚下一步点哪 -->
      <div
        v-if="state && ['available', 'downloading', 'downloaded'].includes(state.status)"
        class="mt-3.5 rounded-[10px] border border-line bg-line-soft/60 p-3"
      >
        <div class="flex items-center justify-between gap-3">
          <div class="min-w-0">
            <div class="text-[12.5px] font-medium text-ink">
              {{ t("settings.update.newVersion", { version: state.version ?? "" }) }}
            </div>
            <div v-if="state.releaseNotes" class="mt-1 max-h-24 overflow-y-auto whitespace-pre-line text-[11.5px] leading-relaxed text-muted">
              {{ state.releaseNotes }}
            </div>
          </div>

          <button
            v-if="state.status === 'available'"
            type="button"
            class="shrink-0 rounded-[9px] bg-brand px-3 py-1.5 text-[12.5px] font-medium text-white transition-opacity hover:opacity-90"
            @click="download"
          >
            <Download :size="13" class="mr-1 inline-block align-[-2px]" />
            {{ t("settings.update.download") }}
          </button>
          <button
            v-else-if="state.status === 'downloaded'"
            type="button"
            class="shrink-0 rounded-[9px] bg-brand px-3 py-1.5 text-[12.5px] font-medium text-white transition-opacity hover:opacity-90"
            @click="install"
          >
            {{ t("settings.update.restart") }}
          </button>
          <span v-else class="shrink-0 text-[12px] text-muted">{{ state.percent }}%</span>
        </div>

        <div v-if="state.status === 'downloading'" class="mt-2.5 h-1 overflow-hidden rounded-full bg-line">
          <div class="h-full rounded-full bg-brand transition-[width] duration-300" :style="{ width: `${state.percent}%` }" />
        </div>
      </div>

      <p class="mt-3.5 border-t border-line-soft pt-3 text-[11.5px] leading-relaxed text-muted">
        {{ t("settings.update.note") }}
      </p>
    </SectionCard>

    <SectionCard :title="t('settings.update.envTitle')">
      <dl class="grid grid-cols-[120px_1fr] gap-y-2 text-[12.5px]">
        <dt class="text-muted">{{ t("settings.update.envPlatform") }}</dt>
        <dd class="text-ink">{{ platformLabel }}</dd>
        <dt class="text-muted">{{ t("settings.update.envVersion") }}</dt>
        <dd class="text-ink">{{ state?.currentVersion ?? desktop?.version ?? "—" }}</dd>
        <dt class="text-muted">{{ t("settings.update.envChannel") }}</dt>
        <dd class="text-ink">{{ t("settings.update.envChannelValue") }}</dd>
      </dl>
    </SectionCard>
  </div>
</template>

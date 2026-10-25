<script setup lang="ts">
/**
 * 设置 → 常规 里的「版本与更新」区块（仅桌面端渲染）。
 *
 * 为什么放在常规里而不是单开一个分栏：更新一年也就点几次，但「我现在是什么版本」
 * 是随时会看的。独立分栏让设置左栏多一项消耗注意力，塞进常规反而顺手。
 *
 * 这里不自己判断有没有新版本 —— 状态全由主进程的更新状态机推过来，经
 * `stores/update.ts` 这个单例中转（弹窗和这张卡片看到同一份状态）。本组件
 * 只负责画，不再自己订阅 `update:state`。
 *
 * 完整链路：启动 8 秒（可关）→ 静默查 → 托盘与这里提示 → 弹窗确认下载（带进度）→
 * 下载完弹窗问重启 → `quitAndInstall`。用户选「稍后」的话，退出应用时自动装上
 * （主进程 `autoInstallOnAppQuit = true`）。
 */
import { computed, ref } from "vue";
import { AlertCircle, ArrowUpCircle, CheckCircle2, Download, Loader2, RefreshCw } from "lucide-vue-next";
import SectionCard from "../../components/SectionCard.vue";
import ToggleSwitch from "../../components/ToggleSwitch.vue";
import { t } from "../../lib/i18n";
import { desktop } from "../../lib/desktop";
import { renderReleaseNotes } from "../../lib/releaseNotes";
import {
  checkForUpdates,
  downloadUpdate,
  estimateEtaSeconds,
  formatEta,
  formatRate,
  installUpdate,
  isSkipped,
  toggleAutoCheck,
  unskipVersion,
  updatePrefs,
  updateState,
} from "../../stores/update";

const state = updateState;
const prefs = updatePrefs;
const busy = ref(false);
const savingPrefs = ref(false);

const updating = computed(() => {
  const status = state.value?.status;
  return status === "checking" || status === "downloading";
});

const skipped = computed(() => isSkipped(state.value?.version));

/** 更新说明渲染成 HTML（GitHub 来源是 HTML、yml 来源是 markdown，清洗在 lib 里做）。 */
const notesHtml = computed(() => renderReleaseNotes(state.value?.releaseNotes));

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
      if (skipped.value) {
        return t("settings.update.status.availableSkipped", { version: current.version ?? "" });
      }
      return t("settings.update.status.available", { version: current.version ?? "" });
    case "downloading": {
      // 有速率数据时显示得更详细；否则退化为纯百分比。
      if (current.bytesPerSecond > 0 && current.total > 0) {
        return t("settings.update.status.downloadingDetail", {
          percent: String(current.percent),
          rate: formatRate(current.bytesPerSecond),
          time: formatEta(estimateEtaSeconds(current)),
        });
      }
      return t("settings.update.status.downloading", { percent: String(current.percent) });
    }
    case "downloaded":
      return t("settings.update.status.downloaded", { version: current.version ?? "" });
    case "error":
      // 归类后的错误由弹窗展示；卡片里只给一个简短结论，不把原始英文 message 糊上来。
      return t("settings.update.status.error");
    default:
      return t("settings.update.status.idle");
  }
});

/** 「最近检查」用相对时间，比一串 ISO 串好读。 */
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
    await checkForUpdates();
  } catch {
    /* 主进程查更新本身不抛，这里兜的是窗口正在关闭时 IPC 被拒的情况 */
  } finally {
    busy.value = false;
  }
}

async function download(): Promise<void> {
  if (!desktop) return;
  await downloadUpdate();
}

async function install(): Promise<void> {
  await installUpdate();
}

async function toggleAuto(): Promise<void> {
  if (!desktop || savingPrefs.value) return;
  savingPrefs.value = true;
  try {
    await toggleAutoCheck();
  } finally {
    savingPrefs.value = false;
  }
}
</script>

<template>
  <SectionCard :title="t('settings.update.title')" :subtitle="t('settings.update.subtitle')">
    <div class="flex items-start justify-between gap-4">
      <div class="min-w-0">
        <div class="text-body font-medium text-ink">
          chaierww {{ state?.currentVersion ?? desktop?.version ?? "" }}
        </div>
        <div class="mt-1 flex items-center gap-1.5 text-body-sm text-muted">
          <Loader2 v-if="updating" :size="13" class="shrink-0 animate-spin" />
          <CheckCircle2
            v-else-if="state?.status === 'up-to-date'"
            :size="13"
            class="shrink-0 text-brand"
          />
          <AlertCircle
            v-else-if="state?.status === 'error'"
            :size="13"
            class="shrink-0 text-danger"
          />
          <ArrowUpCircle
            v-else-if="state?.status === 'available' || state?.status === 'downloaded'"
            :size="13"
            class="shrink-0 text-brand"
          />
          <span class="min-w-0">{{ statusText }}</span>
        </div>
        <div v-if="checkedText" class="mt-1 text-caption text-muted">{{ checkedText }}</div>
      </div>

      <button
        type="button"
        class="shrink-0 rounded-control border border-line px-3 py-1.5 text-body-sm text-ink transition-colors hover:bg-line-soft disabled:cursor-not-allowed disabled:opacity-50"
        :disabled="busy || updating"
        @click="check"
      >
        <RefreshCw
          :size="13"
          class="mr-1 inline-block align-[-2px]"
          :class="busy || updating ? 'animate-spin' : ''"
        />
        {{ t("settings.update.check") }}
      </button>
    </div>

    <!-- 有新版本 / 正在下载 / 已下载：一条状态条说清楚下一步点哪 -->
    <div
      v-if="state && ['available', 'downloading', 'downloaded'].includes(state.status)"
      class="mt-3.5 rounded-control border border-line bg-line-soft/60 p-3"
    >
      <div class="flex items-center justify-between gap-3">
        <div class="min-w-0">
          <div class="text-body-sm font-medium text-ink">
            {{ t("settings.update.newVersion", { version: state.version ?? "" }) }}
          </div>
          <div
            v-if="notesHtml"
            class="release-notes mt-1 max-h-24 overflow-y-auto"
            v-html="notesHtml"
          />
        </div>

        <button
          v-if="state.status === 'available'"
          type="button"
          class="shrink-0 rounded-control bg-brand px-3 py-1.5 text-body-sm font-medium text-white transition-opacity hover:opacity-90"
          @click="download"
        >
          <Download :size="13" class="mr-1 inline-block align-[-2px]" />
          {{ t("settings.update.download") }}
        </button>
        <button
          v-else-if="state.status === 'downloaded'"
          type="button"
          class="shrink-0 rounded-control bg-brand px-3 py-1.5 text-body-sm font-medium text-white transition-opacity hover:opacity-90"
          @click="install"
        >
          {{ t("settings.update.restart") }}
        </button>
        <span v-else class="shrink-0 text-body-sm text-muted">{{ state.percent }}%</span>
      </div>

      <!-- 已跳过这个版本：给一个「取消跳过」的出口，否则用户再也想不起怎么把它找回来 -->
      <button
        v-if="state.status === 'available' && skipped"
        type="button"
        class="mt-2 text-body-sm text-brand underline-offset-2 hover:underline"
        @click="unskipVersion"
      >
        {{ t("settings.update.unskip") }}
      </button>

      <div v-if="state.status === 'downloading'" class="mt-2.5 h-1 overflow-hidden rounded-full bg-line">
        <div
          class="h-full rounded-full bg-brand transition-[width] duration-300"
          :style="{ width: `${state.percent}%` }"
        />
      </div>

      <!-- 出错：给「重试」出口，弹窗里也有更细的归类文案 -->
      <button
        v-if="state.status === 'error'"
        type="button"
        class="mt-2 text-body-sm text-brand underline-offset-2 hover:underline"
        @click="check"
      >
        {{ t("settings.update.retry") }}
      </button>
    </div>

    <!-- 自动检查开关：放在卡片里而不是另开一页，用户看到「启动时会静默检查一次」
         这行字时手边就能关掉 -->
    <div class="mt-4 flex items-center gap-3 border-t border-line-soft pt-3.5">
      <div class="min-w-0 flex-1">
        <div class="text-body font-medium text-ink">{{ t("settings.update.autoCheck") }}</div>
        <div class="mt-0.5 text-body-sm text-muted">{{ t("settings.update.autoCheckDesc") }}</div>
      </div>
      <ToggleSwitch
        :model-value="prefs.autoCheck"
        :disabled="savingPrefs"
        @update:model-value="toggleAuto"
      />
    </div>

    <p class="mt-3.5 border-t border-line-soft pt-3 text-caption leading-relaxed text-muted">
      {{ t("settings.update.note") }}
    </p>
    <p class="mt-1.5 text-caption text-muted">
      {{ platformLabel }} · {{ t("settings.update.envChannelValue") }}
    </p>
  </SectionCard>
</template>

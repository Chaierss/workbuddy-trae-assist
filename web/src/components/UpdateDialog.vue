<script setup lang="ts">
import { computed } from "vue";
import { t } from "../lib/i18n";
import { renderReleaseNotes } from "../lib/releaseNotes";
import { formatBytes } from "../api";
import {
  checkForUpdates,
  closeUpdateDialog,
  downloadUpdate,
  errorMessageKey,
  estimateEtaSeconds,
  formatEta,
  formatRate,
  installUpdate,
  isSkipped,
  skipCurrentVersion,
  unskipVersion,
  updateBusy,
  updateDialog,
  updatePrefs,
  updateState,
} from "../stores/update";

/**
 * 自绘的更新弹窗。
 *
 * 以前主进程用 `dialog.showMessageBox` 弹原生框：按钮最多两个、release notes 塞不进、
 * 下载进度只在设置页可见。这里接管成自绘：品牌图标 + 版本号 + 可滚动说明 + 进度条 +
 * 速率/剩余时间 + 错误归类 + 跳过版本。整体以 `ConfirmDialog` 的遮罩风格为基座，
 * 但宽度放宽、结构自定。
 */

const state = computed(() => updateState.value);

/**
 * 更新说明的 HTML。
 *
 * 主进程给的是原始文本：GitHub 来源是 HTML、yml 来源是 markdown，两者都可能在
 * `{{ }}` 里被当成纯文本吐出来（就是以前那块「代码」）。这里统一渲染成 HTML，
 * 清洗在 `lib/releaseNotes.ts` 里做，所以 `v-html` 是安全的。
 */
const notesHtml = computed(() => renderReleaseNotes(state.value?.releaseNotes));

/** 当前这个版本是否被跳过。 */
const skipped = computed(() => isSkipped(state.value?.version));

/** 弹窗标题，按当前状态挑。 */
const title = computed(() => {
  switch (updateDialog.value) {
    case "downloading":
      return t("update.dialog.titleDownloading");
    case "downloaded":
      return t("update.dialog.titleDownloaded");
    case "error":
      return t("update.dialog.titleError");
    default:
      return t("update.dialog.titleAvailable");
  }
});

const status = computed(() => state.value?.status ?? "idle");

/** 主按钮文案与动作。 */
const primaryLabel = computed(() => {
  if (updateDialog.value === "error") return t("update.dialog.retry");
  if (updateDialog.value === "downloaded") return t("update.dialog.install");
  if (skipped.value) return t("update.dialog.downloadAnyway");
  return t("update.dialog.download");
});

async function onPrimary(): Promise<void> {
  if (updateDialog.value === "error") {
    await checkForUpdates();
    return;
  }
  if (updateDialog.value === "downloaded") {
    await installUpdate();
    return;
  }
  await downloadUpdate();
}

/** 错误态的分类文案。 */
const errorText = computed(() => {
  const kind = state.value?.errorKind ?? "unknown";
  return t(errorMessageKey(kind));
});

const eta = computed(() => {
  if (!state.value) return null;
  return formatEta(estimateEtaSeconds(state.value));
});

function viewRelease(): void {
  // release notes 里看不够，跳到 GitHub 这次发布。desktop.ts 里 desktop 有 platform 但无 repo 信息，
  // 这里直接拼固定仓库地址，窗口打开由主进程 setWindowOpenHandler 交给系统浏览器。
  const version = state.value?.version;
  if (version) {
    window.open(
      `https://github.com/Chaierss/workbuddy-trae-assist/releases/tag/v${version}`,
      "_blank",
    );
  }
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="updateDialog"
      class="fixed inset-0 z-50 grid place-items-center bg-black/25 p-4"
      @click.self="closeUpdateDialog"
    >
      <div class="w-full max-w-[520px] rounded-[16px] border border-line bg-surface p-6">
        <div class="flex items-start gap-3">
          <img
            src="/android-chrome-192x192.png"
            alt=""
            class="size-11 shrink-0 rounded-[12px] border border-line-soft bg-white object-contain p-1"
          />
          <div class="min-w-0 flex-1">
            <h3 class="text-[15px] font-semibold text-ink">{{ title }}</h3>
            <p v-if="state?.version" class="mt-0.5 text-[12px] text-muted">
              {{
                t("update.dialog.versionFromTo", {
                  current: state.currentVersion,
                  next: state.version,
                })
              }}
            </p>
          </div>
        </div>

        <!-- 跳过提示 -->
        <div
          v-if="skipped"
          class="mt-3 rounded-[9px] border border-warn/30 bg-warn-soft/60 px-3 py-2 text-[12px] text-warn"
        >
          {{ t("update.dialog.skippedNote", { version: state?.version ?? "" }) }}
        </div>

        <!-- 更新说明：HTML 渲染（内容已白名单清洗），空则退回占位文案 -->
        <div v-if="updateDialog === 'available' || updateDialog === 'downloaded'" class="mt-4">
          <div class="text-[12px] font-medium text-ink-soft">{{ t("update.dialog.notes") }}</div>
          <div
            v-if="notesHtml"
            class="release-notes mt-1.5 max-h-[220px] overflow-y-auto"
            v-html="notesHtml"
          />
          <div v-else class="mt-1.5 text-[12.5px] leading-relaxed text-ink-soft">
            {{ t("update.dialog.noNotes") }}
          </div>
          <button
            type="button"
            class="mt-1.5 text-[12px] text-brand underline-offset-2 hover:underline"
            @click="viewRelease"
          >
            {{ t("update.dialog.viewRelease") }}
          </button>
        </div>

        <!-- 下载进度 -->
        <div v-if="updateDialog === 'downloading'" class="mt-4">
          <div class="flex items-baseline justify-between">
            <span class="text-[26px] font-semibold tabular-nums text-ink">
              {{ state?.percent ?? 0 }}%
            </span>
            <span class="text-[12px] tabular-nums text-muted">
              {{ formatBytes(state?.transferred ?? 0) }} / {{ formatBytes(state?.total ?? 0) }}
            </span>
          </div>
          <div class="mt-2 h-1.5 overflow-hidden rounded-full bg-line">
            <div
              class="h-full rounded-full bg-brand transition-[width] duration-300"
              :style="{ width: `${state?.percent ?? 0}%` }"
            />
          </div>
          <div class="mt-2 flex items-center justify-between text-[11.5px] text-muted">
            <span>{{ formatRate(state?.bytesPerSecond ?? 0) }}</span>
            <span>{{ eta }}</span>
          </div>
        </div>

        <!-- 错误 -->
        <div v-if="updateDialog === 'error'" class="mt-4">
          <p class="text-[13px] leading-relaxed text-ink-soft">{{ errorText }}</p>
          <details v-if="state?.message" class="mt-2">
            <summary class="cursor-pointer text-[12px] text-muted">
              {{ t("update.dialog.details") }}
            </summary>
            <p class="mt-1.5 break-all font-mono text-[11px] leading-relaxed text-muted">
              {{ state.message }}
            </p>
          </details>
        </div>

        <!-- 已下载：安装说明 -->
        <p v-if="updateDialog === 'downloaded'" class="mt-3 text-[12px] text-muted">
          {{ t("update.dialog.installNote") }}
        </p>

        <div class="mt-5 flex items-center justify-between gap-2">
          <!-- 左侧：次要动作（跳过 / 取消跳过 / 后台） -->
          <div class="flex items-center gap-1">
            <button
              v-if="updateDialog === 'available' && !skipped"
              type="button"
              class="rounded-[9px] px-3 py-2 text-[12.5px] text-ink-soft transition-colors hover:bg-line-soft hover:text-ink"
              :disabled="updateBusy"
              @click="skipCurrentVersion"
            >
              {{ t("update.dialog.skip") }}
            </button>
            <button
              v-if="updateDialog === 'available' && skipped"
              type="button"
              class="rounded-[9px] px-3 py-2 text-[12.5px] text-ink-soft transition-colors hover:bg-line-soft hover:text-ink"
              :disabled="updateBusy"
              @click="unskipVersion"
            >
              {{ t("update.dialog.unskip") }}
            </button>
            <button
              v-if="updateDialog === 'downloading'"
              type="button"
              class="rounded-[9px] px-3 py-2 text-[12.5px] text-ink-soft transition-colors hover:bg-line-soft hover:text-ink"
              @click="closeUpdateDialog"
            >
              {{ t("update.dialog.background") }}
            </button>
          </div>

          <!-- 右侧：主按钮 + 关闭 -->
          <div class="flex items-center gap-2">
            <button
              v-if="updateDialog !== 'downloaded'"
              type="button"
              class="rounded-[9px] border border-line px-3.5 py-2 text-[12.5px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink"
              :disabled="updateBusy"
              @click="closeUpdateDialog"
            >
              {{ updateDialog === "error" ? t("update.dialog.close") : t("update.dialog.later") }}
            </button>
            <button
              type="button"
              class="rounded-[9px] bg-brand px-3.5 py-2 text-[12.5px] font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-60"
              :disabled="updateBusy"
              @click="onPrimary"
            >
              {{ primaryLabel }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

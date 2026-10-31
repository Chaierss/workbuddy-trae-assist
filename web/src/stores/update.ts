import { computed, ref } from "vue";
import { t } from "../lib/i18n";
import { formatBytes } from "../api";
import {
  desktop,
  type DesktopUpdateErrorKind,
  type DesktopUpdatePrefs,
  type DesktopUpdateState,
} from "../lib/desktop";

/**
 * 更新状态单例 store。
 *
 * 主进程是更新状态机（检查/下载/安装都在 `desktop/main.js`），这里只做两件事：
 * 1. 把主进程推来的 `update:state` 收进 ref，供设置卡片、侧栏徽标、弹窗共同消费；
 * 2. 把「该弹弹窗」的 `update:prompt` 事件收进 `updateDialog`，由 `UpdateDialog.vue` 渲染。
 *
 * 为什么用单例而不是让 `UpdateSection.vue` 自己订阅：弹窗和设置卡片要看到**同一份**状态，
 * 各自订阅会让下载进度在卡片与弹窗之间不同步。
 */

export const updateState = ref<DesktopUpdateState | null>(null);
export const updatePrefs = ref<DesktopUpdatePrefs>({ autoCheck: true, skippedVersion: null });

/** 弹窗种类：null = 不弹。值对应主进程 `sendPrompt(kind)` 的 kind。 */
export const updateDialog = ref<string | null>(null);
/** 弹窗里的主按钮是否在忙（下载/安装进行中）。 */
export const updateBusy = ref(false);

let initialized = false;

/** 幂等初始化：只跑一次。先订阅再拉初值，避免主进程推得快时漏掉。 */
export function initUpdateStore(): void {
  if (initialized || !desktop) return;
  initialized = true;

  desktop.update.onState((next) => {
    updateState.value = next;
  });
  desktop.update.onPrompt(({ kind }) => {
    updateDialog.value = kind;
  });

  void (async () => {
    try {
      updateState.value = await desktop.update.get();
      updatePrefs.value = await desktop.update.getPrefs();
    } catch {
      /* 取不到就留空，主进程下一次推送会纠正 */
    }
  })();
}

/** 用户是不是已经跳过当前这个版本。 */
export function isSkipped(version: string | null | undefined): boolean {
  if (!version || !updatePrefs.value.skippedVersion) return false;
  return updatePrefs.value.skippedVersion === version;
}

/** 侧栏「设置」按钮上的更新小圆点：有待处理的更新才亮。 */
export const updateBadge = computed(() => {
  const s = updateState.value;
  if (!s) return false;
  if (s.status === "downloaded") return true;
  if (s.status === "available") return !isSkipped(s.version);
  return false;
});

export function openUpdateDialog(kind: string | null): void {
  updateDialog.value = kind;
}

export function closeUpdateDialog(): void {
  updateDialog.value = null;
}

export async function checkForUpdates(): Promise<void> {
  if (!desktop) return;
  const next = await desktop.update.check();
  updateState.value = next;
  // 手动检查现在也是「发现即静默下载」，不再弹「要不要下载」。
  // 只有被跳过的版本需要问一句（弹窗带「取消跳过」入口，主进程也会推同一个 kind）；
  // 已是最新就只更新卡片，不弹「已是最新」这种废话框。
  if (next.status === "available" && isSkipped(next.version)) {
    updateDialog.value = "available";
  } else if (next.status === "error") {
    updateDialog.value = "error";
  }
}

export async function downloadUpdate(): Promise<void> {
  if (!desktop || updateBusy.value) return;
  updateBusy.value = true;
  try {
    updateState.value = await desktop.update.download();
    // 进入 downloading 后弹窗继续显示进度，别关掉。
    if (updateState.value.status === "downloading") updateDialog.value = "downloading";
  } finally {
    updateBusy.value = false;
  }
}

export async function installUpdate(): Promise<void> {
  if (!desktop || updateBusy.value) return;
  updateBusy.value = true;
  try {
    await desktop.update.install();
  } finally {
    updateBusy.value = false;
  }
}

export async function skipCurrentVersion(): Promise<void> {
  if (!desktop) return;
  const version = updateState.value?.version;
  if (!version) return;
  updatePrefs.value = await desktop.update.skip(version);
}

export async function unskipVersion(): Promise<void> {
  if (!desktop) return;
  updatePrefs.value = await desktop.update.unskip();
}

export async function toggleAutoCheck(): Promise<void> {
  if (!desktop) return;
  updatePrefs.value = await desktop.update.setPrefs({
    autoCheck: !updatePrefs.value.autoCheck,
  });
}

/** 把主进程归类的错误映射成人话的 key，文案在 i18n 里。返回字面量联合，保证 `t()` 能过类型检查。 */
export function errorMessageKey(kind: DesktopUpdateErrorKind):
  | "update.dialog.errorNetwork"
  | "update.dialog.errorNoRelease"
  | "update.dialog.errorDownload"
  | "update.dialog.errorChecksum"
  | "update.dialog.errorDisk"
  | "update.dialog.errorUnknown" {
  switch (kind) {
    case "network":
      return "update.dialog.errorNetwork";
    case "noRelease":
      return "update.dialog.errorNoRelease";
    case "download":
      return "update.dialog.errorDownload";
    case "checksum":
      return "update.dialog.errorChecksum";
    case "disk":
      return "update.dialog.errorDisk";
    default:
      return "update.dialog.errorUnknown";
  }
}

/** 剩余时间估算。速率未知（0）时返回 null，由调用方决定显示「正在估算」。 */
export function estimateEtaSeconds(state: DesktopUpdateState): number | null {
  const { total, transferred, bytesPerSecond } = state;
  if (!total || !bytesPerSecond || transferred >= total) return null;
  return Math.ceil((total - transferred) / bytesPerSecond);
}

export function formatEta(seconds: number | null): string {
  if (seconds === null) return t("update.dialog.remainingUnknown");
  if (seconds < 60) return t("update.dialog.etaSeconds", { count: String(seconds) });
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return t("update.dialog.etaMinutes", { count: String(m), seconds: String(s) });
}

export function formatRate(bytesPerSecond: number): string {
  return `${formatBytes(bytesPerSecond)}/s`;
}

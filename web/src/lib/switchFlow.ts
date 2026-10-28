import { reactive } from "vue";
import {
  api,
  type Account,
  type ClientSlot,
  type MigrationPreview,
  type SwitchReport,
} from "../api";
import { clientDetections, refreshAll } from "../stores/app";
import { refreshOnboarding } from "./onboarding";
import { track } from "./metrics";

/**
 * 换号流程的唯一状态机。
 *
 * 号池卡片、命令面板、桌面托盘三个入口都调 `openSwitch()`，确认后统一走
 * `confirmSwitch()`。弹层挂在 AppShell 上，所以切换过程中离开当前页也不会丢。
 */
export type SwitchMethod = "manual" | "command" | "tray";

export const switchFlow = reactive({
  open: false,
  busy: false,
  report: null as SwitchReport | null,
  error: null as string | null,
  /** 迁移预检结果（确认态）。 */
  preview: null as MigrationPreview | null,
  /** true = 处于「预检确认」态，尚未真正切换。 */
  confirming: false,
  /** 待切换的账号（确认态需要它来执行切换）。 */
  account: null as Account | null,
  /** 目标账号所属平台登记的程序位（只读 / 未适配的位置会灰掉）。 */
  slots: [] as ClientSlot[],
  /** 选中的程序位 key；空串 = 该平台不区分程序位，按默认位切换。 */
  slot: "",
  /** 本次流程的入口，埋点用。 */
  method: "manual" as SwitchMethod,
});

/**
 * 打开预检弹层并拉取只读预检。
 * 预检本身就是「确认步骤」，所以不再走通用 ConfirmDialog（那会变成连点两次确认）。
 */
export async function openSwitch(account: Account, method: SwitchMethod = "manual"): Promise<void> {
  switchFlow.open = true;
  switchFlow.account = account;
  switchFlow.method = method;
  switchFlow.report = null;
  switchFlow.error = null;
  switchFlow.preview = null;
  switchFlow.confirming = true;
  // 程序位来自该平台最新的检测结果；默认选第一个**可写**的位。
  switchFlow.slots =
    clientDetections.value.find((item) => item.scope === account.scope)?.slots ?? [];
  switchFlow.slot = switchFlow.slots.find((item) => item.writable)?.key ?? "";
  switchFlow.busy = true;
  try {
    switchFlow.preview = await api.migrationPreview(account.id, account.scope);
    track("switch_previewed", { platform: account.platform, method });
  } catch (error) {
    switchFlow.error = (error as Error).message;
  } finally {
    switchFlow.busy = false;
  }
}

/** 预检确认后，真正执行切换。 */
export async function confirmSwitch(): Promise<void> {
  const account = switchFlow.account;
  if (!account) return;
  switchFlow.confirming = false;
  switchFlow.busy = true;
  switchFlow.report = null;
  switchFlow.error = null;
  try {
    switchFlow.report = await api.switchAccount(
      account.id,
      undefined,
      true,
      switchFlow.slot || undefined,
    );
    if (switchFlow.report?.succeeded) {
      track("switch_completed", { platform: account.platform, method: switchFlow.method });
      // firstSwitchAt 由切换引擎写入，本地快照要跟上，清单才会打勾。
      void refreshOnboarding();
    }
    await refreshAll();
  } catch (error) {
    switchFlow.error = (error as Error).message;
  } finally {
    switchFlow.busy = false;
  }
}

/** 关闭切换弹层并复位状态。 */
export function closeSwitch(): void {
  switchFlow.open = false;
  switchFlow.confirming = false;
  switchFlow.preview = null;
  switchFlow.report = null;
  switchFlow.error = null;
  switchFlow.account = null;
  switchFlow.slots = [];
  switchFlow.slot = "";
}

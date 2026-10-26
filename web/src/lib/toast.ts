import { ref } from "vue";

/**
 * 全局轻提示：右上角、队列、4 秒自动消失；错误留在原地等用户关。
 *
 * 以前只有号池页有局部 toast，其它页用内联 banner —— 同一个动作在不同页面的
 * 反馈位置不一样。全部收敛到这里，读屏通过 `aria-live` 播报。
 */

export type ToastTone = "success" | "error" | "info";

export interface ToastItem {
  id: number;
  message: string;
  tone: ToastTone;
  /** 0 = 不自动消失（错误默认如此）。 */
  timeout: number;
}

const DEFAULT_TIMEOUT = 4000;
const MAX_VISIBLE = 3;

export const toasts = ref<ToastItem[]>([]);

let nextId = 1;
const timers = new Map<number, number>();

export function dismissToast(id: number): void {
  const timer = timers.get(id);
  if (timer !== undefined) {
    window.clearTimeout(timer);
    timers.delete(id);
  }
  toasts.value = toasts.value.filter((item) => item.id !== id);
}

export function pushToast(
  message: string,
  options: { tone?: ToastTone; timeout?: number } = {},
): number {
  const tone = options.tone ?? "info";
  const timeout =
    options.timeout ?? (tone === "error" ? 0 : DEFAULT_TIMEOUT);
  const id = nextId++;
  toasts.value = [...toasts.value, { id, message, tone, timeout }].slice(-MAX_VISIBLE);
  if (timeout > 0) {
    timers.set(
      id,
      window.setTimeout(() => dismissToast(id), timeout),
    );
  }
  return id;
}

/** 成功反馈：动作已经完成，不需要用户再做什么。 */
export function toastSuccess(message: string): number {
  return pushToast(message, { tone: "success" });
}

/** 失败反馈：停在屏幕上等用户读，不自动消失。 */
export function toastError(message: string): number {
  return pushToast(message, { tone: "error" });
}

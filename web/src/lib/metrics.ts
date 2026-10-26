import { api } from "../api";

/**
 * 本地体验数据（只在本机，不上传）。
 *
 * 内存队列 + 批量提交：15s 或攒到 20 条就发一次；页面隐藏时立即 flush。
 * 任何失败都静默丢弃 —— 度量永远不能打断交互，也不能让界面出现红色报错。
 *
 * 事件名与属性都受后端白名单约束（`domain/metrics.py`）。**不要**往这里塞
 * 账号 id、邮箱、路径、prompt 等任何自由文本：后端会丢，而且丢之前已经过了网络。
 */

export interface MetricEvent {
  name: string;
  props?: Record<string, string | number | boolean>;
}

const FLUSH_INTERVAL_MS = 15_000;
const FLUSH_BATCH = 20;

let queue: MetricEvent[] = [];
let timer: number | null = null;
let flushing = false;
/** 后端开关的本地副本：关闭时连队列都不进，避免无意义的请求。 */
let enabled = true;

export function setMetricsEnabled(next: boolean): void {
  enabled = next;
  if (!enabled) {
    queue = [];
    clearTimer();
  }
}

export function metricsEnabled(): boolean {
  return enabled;
}

export function track(name: string, props?: MetricEvent["props"]): void {
  if (!enabled) return;
  queue.push({ name, props });
  if (queue.length >= FLUSH_BATCH) {
    void flush();
    return;
  }
  schedule();
}

/** 立即提交队列；可安全并发调用。 */
export async function flush(): Promise<void> {
  if (flushing || !queue.length) return;
  flushing = true;
  clearTimer();
  const items = queue;
  queue = [];
  try {
    // 串行发送：本地回环，量很小；并发反而容易在退出时丢后半截。
    for (const item of items) {
      await api.recordMetric(item.name, item.props).catch(() => undefined);
    }
  } finally {
    flushing = false;
    if (queue.length) schedule();
  }
}

function schedule(): void {
  if (timer !== null) return;
  timer = window.setTimeout(() => {
    timer = null;
    void flush();
  }, FLUSH_INTERVAL_MS);
}

function clearTimer(): void {
  if (timer === null) return;
  window.clearTimeout(timer);
  timer = null;
}

/** 页面隐藏 / 即将卸载时兜底提交（`sendBeacon` 不适用：需要 JSON 与回环鉴权头）。 */
if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") void flush();
  });
}

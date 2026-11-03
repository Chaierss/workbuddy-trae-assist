import { ref } from "vue";

/**
 * 断线感知：连续 2 次请求网络层失败才判定「后端没响应」。
 *
 * 只认 `fetch` 抛出的网络错误；HTTP 4xx/5xx 说明后端还活着，算正常。
 * 阈值取 2 是为了躲开切号 / 重启客户端瞬间的单次抖动，避免误报横幅。
 */
const FAILURE_THRESHOLD = 2;

let failures = 0;

/** 后端是否已经连续失败到「应该提示用户」的程度。 */
export const backendOffline = ref(false);

export function reportRequestResult(ok: boolean): void {
  if (ok) {
    failures = 0;
    backendOffline.value = false;
    return;
  }
  failures += 1;
  if (failures >= FAILURE_THRESHOLD) {
    backendOffline.value = true;
  }
}

/** 测试与「用户点了重试」用：清零计数，让下一次成功即可恢复正常。 */
export function resetHealthWatch(): void {
  failures = 0;
  backendOffline.value = false;
}

import { computed, ref } from "vue";

/**
 * 全局任务中心：把「复检 / 续期 / 备份 / 跑活动 / 立即执行计划」收进一条可追踪的流水。
 *
 * 边界：
 * - 只做**结果回执**，不做进度百分比：这些动作都没有可靠的百分比来源，编一个数字只会骗人；
 * - 「进行中」只在当前进程内有效，刷新后不复活（这些动作不可续跑）；
 * - 最近 20 条终态回执存 localStorage，刷新后仍能回看；
 * - 同一动作进行中时不允许重复发起，避免连点把上游打两遍。
 */

export type TaskStatus = "queued" | "running" | "ok" | "failed";

export interface TaskItem {
  id: string;
  kind: string;
  title: string;
  status: TaskStatus;
  startedAt: number;
  finishedAt: number | null;
  message: string;
  /** 失败时保留重试入口；只在当前进程内有效。 */
  retry?: () => Promise<unknown>;
}

export interface TaskReceipt {
  id: string;
  kind: string;
  title: string;
  status: Exclude<TaskStatus, "queued" | "running">;
  startedAt: number;
  finishedAt: number;
  message: string;
}

const STORAGE_KEY = "chaierww.tasks.receipts";
const MAX_RECEIPTS = 20;

function loadReceipts(): TaskReceipt[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as TaskReceipt[];
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(
        (item) =>
          item &&
          typeof item.id === "string" &&
          (item.status === "ok" || item.status === "failed"),
      )
      .slice(0, MAX_RECEIPTS);
  } catch {
    return [];
  }
}

/** 进行中 + 失败（当前进程）+ 历史回执，统一按时间倒序展示。 */
export const taskItems = ref<Array<TaskItem | TaskReceipt>>(loadReceipts());
export const taskDrawerOpen = ref(false);

const activeKeys = new Set<string>();
/** 去重按动作类别，不按标题：同一动作在页面与命令面板的标题可能不同。 */
const keyOf = (kind: string) => kind;

export const runningCount = computed(
  () =>
    taskItems.value.filter(
      (item) => item.status === "running" || item.status === "queued",
    ).length,
);
export const failedCount = computed(
  () => taskItems.value.filter((item) => item.status === "failed").length,
);

function persist(): void {
  const receipts = taskItems.value
    .filter(
      (item): item is TaskItem =>
        item.status === "ok" || item.status === "failed",
    )
    .map((item) => ({
      id: item.id,
      kind: item.kind,
      title: item.title,
      status: item.status as TaskReceipt["status"],
      startedAt: item.startedAt,
      finishedAt: item.finishedAt ?? Date.now(),
      message: item.message,
    }))
    .slice(0, MAX_RECEIPTS);
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(receipts));
  } catch {
    // 本地存储不可用只是回执丢失，不影响动作本身。
  }
}

function prune(): void {
  const terminal = taskItems.value.filter(
    (item) => item.status === "ok" || item.status === "failed",
  );
  if (terminal.length > MAX_RECEIPTS) {
    const keep = new Set(terminal.slice(0, MAX_RECEIPTS).map((item) => item.id));
    taskItems.value = taskItems.value.filter(
      (item) => !keep.has(item.id) || !(item.status === "ok" || item.status === "failed"),
    );
  }
}

function newId(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }
}

/**
 * 执行一个可追踪动作。动作本身抛错时会写失败回执并**继续向上抛**，
 * 让调用方保留原有错误提示；任务中心只负责留痕与重试。
 */
export async function runTask<T>(options: {
  kind: string;
  title: string;
  run: () => Promise<T>;
  message?: string;
}): Promise<T> {
  const key = keyOf(options.kind);
  if (activeKeys.has(key)) {
    throw new Error("这个操作正在进行中，请等它结束");
  }
  activeKeys.add(key);
  const id = newId();
  const item: TaskItem = {
    id,
    kind: options.kind,
    title: options.title,
    status: "running",
    startedAt: Date.now(),
    finishedAt: null,
    message: "",
  };
  taskItems.value = [item, ...taskItems.value];
  try {
    const result = await options.run();
    item.status = "ok";
    item.finishedAt = Date.now();
    item.message = options.message ?? "";
    item.retry = undefined;
    return result;
  } catch (cause) {
    item.status = "failed";
    item.finishedAt = Date.now();
    item.message = (cause as Error)?.message || "执行失败";
    item.retry = async () => {
      taskItems.value = taskItems.value.filter((row) => row.id !== id);
      return runTask(options);
    };
    throw cause;
  } finally {
    activeKeys.delete(key);
    prune();
    persist();
  }
}

/** 重试一条失败回执：保留原入口，失败后仍可再次重试。 */
export async function retryTask(id: string): Promise<void> {
  const item = taskItems.value.find((row) => row.id === id);
  if (!item || !("retry" in item) || !item.retry) return;
  await item.retry();
}

export function clearFinished(): void {
  taskItems.value = taskItems.value.filter(
    (item) => item.status === "running" || item.status === "queued",
  );
  persist();
}

export function heuristicallyActive(kind: string): boolean {
  return activeKeys.has(keyOf(kind));
}

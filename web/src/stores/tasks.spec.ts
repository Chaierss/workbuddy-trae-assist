import { beforeEach, describe, expect, it } from "vitest";
import {
  clearFinished,
  failedCount,
  retryTask,
  runTask,
  taskItems,
} from "./tasks";

describe("任务中心", () => {
  beforeEach(() => {
    clearFinished();
  });

  it("成功与失败都留下回执，失败可重试", async () => {
    await runTask({
      kind: "backup",
      title: "创建备份",
      run: async () => "ok",
    });
    let attempts = 0;
    const failing = runTask({
      kind: "credential-refresh",
      title: "续期凭据",
      run: async () => {
        attempts += 1;
        if (attempts === 1) throw new Error("上游 502");
        return "ok";
      },
    });
    await expect(failing).rejects.toThrow("上游 502");
    expect(failedCount.value).toBe(1);

    const failed = taskItems.value.find((item) => item.status === "failed");
    expect(failed?.message).toBe("上游 502");
    if (failed) await retryTask(failed.id);
    expect(failedCount.value).toBe(0);
  });

  it("同一动作连续成功只保留最近一条回执", async () => {
    for (const round of [1, 2, 3]) {
      await runTask({
        kind: "activity-run",
        title: "执行活动",
        run: async () => round,
        message: `第 ${round} 次`,
      });
    }
    const receipts = taskItems.value.filter(
      (item) => item.status === "ok" && item.kind === "activity-run",
    );
    expect(receipts).toHaveLength(1);
    expect(receipts[0].message).toBe("第 3 次");
  });

  it("同一动作进行中不允许重复发起", async () => {
    const gate: { release: () => void } = { release: () => undefined };
    const first = runTask({
      kind: "activity-run",
      title: "执行活动",
      run: () =>
        new Promise<void>((resolve) => {
          gate.release = resolve;
        }),
    });
    await expect(
      runTask({
        kind: "activity-run",
        title: "执行活动",
        run: async () => undefined,
      }),
    ).rejects.toThrow("正在进行中");
    gate.release();
    await first;
  });
});

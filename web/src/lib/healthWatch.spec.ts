import { beforeEach, describe, expect, it } from "vitest";
import { backendOffline, reportRequestResult, resetHealthWatch } from "./healthWatch";

describe("healthWatch", () => {
  beforeEach(() => resetHealthWatch());

  it("一次网络失败不误报", () => {
    reportRequestResult(false);
    expect(backendOffline.value).toBe(false);
  });

  it("连续两次失败才判定离线", () => {
    reportRequestResult(false);
    reportRequestResult(false);
    expect(backendOffline.value).toBe(true);
  });

  it("成功一次立刻恢复", () => {
    reportRequestResult(false);
    reportRequestResult(false);
    reportRequestResult(true);
    expect(backendOffline.value).toBe(false);
  });

  it("HTTP 错误也算后端活着", () => {
    reportRequestResult(false);
    reportRequestResult(true);
    reportRequestResult(false);
    expect(backendOffline.value).toBe(false);
  });
});

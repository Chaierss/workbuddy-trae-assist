// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

/**
 * 引导步骤的顺序是持久化契约：`onboardingCompleted` 里存的是步骤 id，
 * 顺序决定清单的呈现与进度条；调整顺序必须同时改版本号，否则老用户的
 * 已完成集合会被新的顺序「重排」成怪状态。
 */

// onboarding.ts 会经 stores/app 读主题偏好，jsdom 没有 matchMedia，先补桩再动态导入。
window.matchMedia = ((query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addListener: () => {},
  removeListener: () => {},
  addEventListener: () => {},
  removeEventListener: () => {},
  dispatchEvent: () => false,
})) as typeof window.matchMedia;

const { ONBOARDING_STEPS, ONBOARDING_VERSION } = await import("./onboarding");

describe("onboarding", () => {
  it("keeps the documented step order", () => {
    expect([...ONBOARDING_STEPS]).toEqual([
      "install",
      "signin",
      "first_switch",
      "retention",
    ]);
  });

  it("exposes a positive version for the persisted state machine", () => {
    expect(ONBOARDING_VERSION).toBeGreaterThan(0);
  });
});

import { expect, test } from "@playwright/test";

test("概览与活动页可用（API 守卫下自动领取会话）", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();

  await page.goto("/activity");
  await expect(page.getByRole("heading", { name: "活动", exact: false }).first()).toBeVisible();
  // 官方活动是默认标签：要么有卡片，要么是明确的空态，不能白屏。
  await expect(
    page.getByText(/官方活动|还没有可展示的官方活动|No official activities/).first(),
  ).toBeVisible();
});

test("设置页能打开诊断卡片", async ({ page }) => {
  await page.goto("/settings?tab=env");
  await expect(page.getByText(/诊断与支持|Diagnostics & support/)).toBeVisible();
  await expect(page.getByText(/导出诊断包|Export diagnostics/)).toBeVisible();
});

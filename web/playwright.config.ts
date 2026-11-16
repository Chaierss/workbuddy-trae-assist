import { defineConfig } from "@playwright/test";

/**
 * 冒烟 E2E：真实后端 + Vite dev（代理 /api）。
 * 后端数据目录用仓库内临时目录，测试结束可随时删除。
 *
 * 端口默认仍是 57910 / 57911；本机已经跑着正式客户端时，用
 * ``E2E_API_PORT=57930 E2E_WEB_PORT=57931`` 换一对端口，避免和桌面端抢监听。
 */
const API_PORT = process.env.E2E_API_PORT ?? "57910";
const WEB_PORT = process.env.E2E_WEB_PORT ?? "57911";
const WEB_URL = `http://127.0.0.1:${WEB_PORT}`;

export default defineConfig({
  testDir: "./e2e",
  // 用例总时长要容得下「首个断言等冷启动」的 45s 预算，否则断言还没超时就先被用例超时掐掉。
  timeout: 90_000,
  retries: 0,
  // 首次冷启动时 Vite 预构建 + 后端首次读写库都偏慢，默认 5s 断言超时会假失败。
  expect: { timeout: 15_000 },
  use: {
    baseURL: WEB_URL,
    trace: "retain-on-failure",
    // 没下载 Playwright 自带 Chromium 时，可用本机 Chrome / Edge 跑：
    // `E2E_CHANNEL=chrome npm run test:e2e`。默认仍用随包 Chromium，CI 行为不变。
    channel: (process.env.E2E_CHANNEL as "chrome" | "msedge" | undefined) || undefined,
  },
  webServer: [
    {
      command: "python -m chaierww.main",
      cwd: "..",
      url: `http://127.0.0.1:${API_PORT}/api/health`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        PYTHONPATH: "src",
        CHAIERWW_PORT: API_PORT,
        // 相对后端 cwd（仓库根目录），落在仓库内的临时目录里，便于清理。
        CHAIERWW_HOME: process.env.CHAIERWW_HOME ?? ".tmp-e2e",
      },
    },
    {
      command: `npm run dev -- --port ${WEB_PORT}`,
      url: WEB_URL,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        CHAIERWW_API: `http://127.0.0.1:${API_PORT}`,
      },
    },
  ],
});

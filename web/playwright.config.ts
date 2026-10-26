import { defineConfig } from "@playwright/test";

/**
 * 冒烟 E2E：真实后端 + Vite dev（代理 /api）。
 * 后端数据目录用仓库内临时目录，测试结束可随时删除。
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  retries: 0,
  use: {
    baseURL: "http://127.0.0.1:57911",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: "python -m chaierww.main",
      cwd: "..",
      url: "http://127.0.0.1:57910/api/health",
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        PYTHONPATH: "src",
        // 相对后端 cwd（仓库根目录），落在仓库内的临时目录里，便于清理。
        CHAIERWW_HOME: process.env.CHAIERWW_HOME ?? ".tmp-e2e",
      },
    },
    {
      command: "npm run dev",
      url: "http://127.0.0.1:57911",
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});

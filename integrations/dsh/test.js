import assert from "node:assert/strict";
import { join } from "node:path";
import test from "node:test";

import {
  buildActivitiesReport,
  buildPool,
  buildSwitchReport,
  buildUsage,
  normalizeBaseUrl,
  renderActivities,
  renderPool,
  renderSwitch,
  renderUsage,
  resolveTokenPath,
} from "./lib.js";

test("normalizeBaseUrl：只接受回环地址，并收敛成 origin", () => {
  assert.equal(normalizeBaseUrl("http://127.0.0.1:57910/"), "http://127.0.0.1:57910");
  assert.equal(
    normalizeBaseUrl("http://localhost:57910/api/"),
    "http://localhost:57910",
  );
  assert.throws(() => normalizeBaseUrl(""), /不能为空/);
  assert.throws(() => normalizeBaseUrl("ftp://127.0.0.1:57910"), /只支持/);
  assert.throws(() => normalizeBaseUrl("http://user:pass@127.0.0.1:57910"), /用户名或密码/);
  assert.throws(() => normalizeBaseUrl("http://example.com:57910"), /回环地址/);
});

test("resolveTokenPath：显式配置 > CHAIERWW_HOME > ~/.chaierww", () => {
  const home = join("C:", "Users", "demo");
  assert.equal(
    resolveTokenPath({ tokenFile: join("D:", "token"), env: {}, home }),
    join("D:", "token"),
  );
  assert.equal(
    resolveTokenPath({ tokenFile: "", env: { CHAIERWW_HOME: join("E:", "cw") }, home }),
    join("E:", "cw", "api-token"),
  );
  assert.equal(
    resolveTokenPath({ tokenFile: "", env: {}, home }),
    join(home, ".chaierww", "api-token"),
  );
});

test("buildPool / renderPool：未知不等于 0，凭据状态可读", () => {
  const expiresAt = Date.UTC(2026, 11, 1) / 1000;
  const payload = buildPool(
    {
      accounts: [
        {
          id: "acc-1",
          displayName: "用户 1001",
          platform: "workbuddy",
          scope: "workbuddy-cn",
          groupName: "主力",
          note: "夜间用",
          credentialState: "usable",
          credentialExpiresAt: "2026-11-20T08:00:00Z",
          credentialRefreshable: true,
        },
        {
          id: "acc-2",
          displayName: "用户 2002",
          platform: "trae",
          scope: "trae-cn",
          credentialState: "relogin_required",
        },
      ],
    },
    {
      credits: {
        "acc-1": {
          available: true,
          credits: 1200,
          total: 2000,
          expiresAt,
          subscription: "Pro",
        },
        "acc-2": { available: false, error: "凭据形态不支持" },
      },
    },
  );

  assert.equal(payload.total, 2);
  assert.equal(payload.accounts[0].credit.credits, 1200);

  const text = renderPool(payload);
  assert.match(text, /号池共 2 个账号/);
  assert.match(text, /acc-1 ｜ 用户 1001 ｜ workbuddy · 主力 ｜ 积分 1200\/2000/);
  assert.match(text, /2026-12-01 到期/);
  assert.match(text, /凭据可用 · 2026-11-20 到期 · 可自动续期/);
  assert.match(text, /备注：夜间用/);
  assert.match(text, /积分查不了（凭据形态不支持）/);
  assert.match(text, /需重新登录/);
  assert.doesNotMatch(text, /undefined/);
});

test("renderPool：没有积分摘要时显示未知，不显示 0", () => {
  const payload = buildPool(
    { accounts: [{ id: "acc-1", displayName: "甲", platform: "workbuddy" }] },
    {},
  );
  assert.equal(payload.accounts[0].credit, null);
  const text = renderPool(payload);
  assert.match(text, /积分未知/);
  assert.doesNotMatch(text, /积分 0/);
});

test("buildUsage / renderUsage：查不了的账号写明原因", () => {
  const payload = buildUsage({
    accounts: [
      {
        accountId: "acc-1",
        displayName: "甲",
        platform: "workbuddy",
        detectable: true,
        todayRequests: 3,
        todayCredit: 1.5,
        monthRequests: 40,
        monthCredit: 20,
      },
      {
        accountId: "acc-2",
        displayName: "乙",
        platform: "trae",
        detectable: false,
        error: "未登录",
      },
    ],
  });
  assert.equal(payload.detectableCount, 1);
  const text = renderUsage(payload);
  assert.match(text, /用量：1\/2 个账号可查/);
  assert.match(text, /今日 3 次 \/ 1.5 积分/);
  assert.match(text, /本月 40 次 \/ 20 积分/);
  assert.match(text, /乙（trae）查不了：未登录/);
});

test("buildSwitchReport / renderSwitch：预演计划与回滚提示", () => {
  const files = Array.from({ length: 14 }, (_, index) => ({
    path: `C:/demo/file-${index}.json`,
    action: "write",
    size: 2048,
    note: "",
  }));
  const preview = buildSwitchReport({
    state: "planned",
    dryRun: true,
    succeeded: true,
    scope: "workbuddy-cn",
    accountId: "acc-1",
    warnings: ["客户端正在运行"],
    plan: { requiresAppExit: true, warnings: ["会覆盖 auth.json"], files },
  });
  const previewText = renderSwitch(preview);
  assert.match(previewText, /切换预演（可执行）/);
  assert.match(previewText, /需要先退出客户端：是/);
  assert.match(previewText, /计划改动 14 个文件/);
  assert.match(previewText, /…还有 2 个文件未列出/);
  assert.match(previewText, /警告：会覆盖 auth\.json/);
  assert.match(previewText, /警告：客户端正在运行/);
  assert.doesNotMatch(previewText, /undefined/);

  const failed = buildSwitchReport({
    state: "failed",
    dryRun: false,
    succeeded: false,
    scope: "workbuddy-cn",
    accountId: "acc-1",
    error: "客户端拒绝退出",
    rollbackFailed: true,
    steps: [{ step: "退出客户端", ok: false, detail: "超时" }],
  });
  const failedText = renderSwitch(failed);
  assert.match(failedText, /切换失败/);
  assert.match(failedText, /错误：客户端拒绝退出/);
  assert.match(failedText, /回滚未成功/);
  assert.match(failedText, /失败：退出客户端（超时）/);
});

test("buildActivitiesReport / renderActivities：跳过分类与失败明细", () => {
  const report = buildActivitiesReport({
    summary: {
      total: 6,
      ok: 3,
      skipped: 2,
      failed: 1,
      retryable: 1,
      "skip.done": 2,
      "skip.upstream": 0,
    },
    results: [
      { accountId: "acc-1", displayName: "甲", title: "每日签到", status: "failed", message: "网络超时" },
      { accountId: "acc-2", displayName: "乙", title: "每日签到", status: "ok" },
    ],
  });
  assert.deepEqual(report.summary.skips, [{ reason: "done", count: 2 }]);
  const text = renderActivities(report);
  assert.match(text, /共 6 项，成功 3，跳过 2，失败 1/);
  assert.match(text, /跳过分类：done 2/);
  assert.match(text, /甲 \/ 每日签到：网络超时/);
  assert.match(text, /其中可重试失败 1 项/);
  assert.doesNotMatch(text, /乙 \/ 每日签到：/);
});

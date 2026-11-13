/**
 * chaierww DSH 插件的纯函数层。
 *
 * 这里只做「校验 / 归一 / 渲染」：不读文件、不发请求、不读时钟，
 * `test.js` 因此可以直接覆盖，不需要起 chaierww 服务。
 */

import { join } from "node:path";

export const DEFAULT_BASE_URL = "http://127.0.0.1:57910";
export const DEFAULT_TIMEOUT_MS = 15_000;
export const MAX_RESPONSE_BYTES = 8 * 1024 * 1024;

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1", "[::1]"]);
const CREDENTIAL_LABELS = {
  usable: "可用",
  unverified: "未验证",
  relogin_required: "需重新登录",
  unknown: "未知",
};

/**
 * 只接受回环地址：本机 API 令牌不能发给任何远端主机。
 * 返回 origin（去路径、去查询、去尾斜杠），后续请求一律基于它拼。
 */
export function normalizeBaseUrl(raw) {
  const value = typeof raw === "string" ? raw.trim() : "";
  if (!value) throw new Error("baseUrl 不能为空");
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`baseUrl 不是合法 URL：${value}`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("baseUrl 只支持 http / https");
  }
  if (url.username || url.password) {
    throw new Error("baseUrl 不允许携带用户名或密码");
  }
  const host = url.hostname.toLowerCase();
  if (!LOOPBACK_HOSTS.has(host)) {
    throw new Error(`baseUrl 必须指向本机回环地址（127.0.0.1 / localhost / ::1），当前为 ${host}`);
  }
  return url.origin;
}

/** 本机 API 令牌位置：显式配置 > CHAIERWW_HOME > ~/.chaierww。 */
export function resolveTokenPath({ tokenFile, env = {}, home }) {
  const explicit = typeof tokenFile === "string" ? tokenFile.trim() : "";
  if (explicit) return explicit;
  const root = (env.CHAIERWW_HOME || "").trim() || join(home, ".chaierww");
  return join(root, "api-token");
}

/** 修剪空白字符串，非字符串一律当空串。 */
function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

function dateOfIso(value) {
  const raw = text(value);
  if (!raw) return "";
  return raw.slice(0, 10);
}

function dateOfEpoch(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return "";
  return new Date(value * 1000).toISOString().slice(0, 10);
}

function num(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function formatSize(bytes) {
  const size = num(bytes);
  if (size <= 0) return "";
  if (size < 1024) return `${size} B`;
  return `${(size / 1024).toFixed(1)} KB`;
}

/* ---------- 号池 ---------- */

function normalizeCredit(raw) {
  if (!raw || typeof raw !== "object") return null;
  return {
    available: Boolean(raw.available),
    credits: typeof raw.credits === "number" ? raw.credits : null,
    total: typeof raw.total === "number" ? raw.total : null,
    expiresAt: typeof raw.expiresAt === "number" ? raw.expiresAt : null,
    expirationText: dateOfEpoch(raw.expiresAt),
    subscription: text(raw.subscription),
    error: text(raw.error),
  };
}

function normalizeAccount(raw, credit) {
  const expiresAt = text(raw.credentialExpiresAt);
  return {
    id: text(raw.id),
    displayName: text(raw.displayName),
    platform: text(raw.platform),
    scope: text(raw.scope),
    group: text(raw.groupName),
    note: text(raw.note),
    credentialState: text(raw.credentialState) || "unknown",
    credentialExpiresAt: expiresAt,
    credentialRefreshable: Boolean(raw.credentialRefreshable),
    credentialDeviceForeign: Boolean(raw.credentialDeviceForeign),
    credit: normalizeCredit(credit),
  };
}

/** 把 `/api/accounts/merged` 与 `/api/accounts/credits` 合成一份稳定结构。 */
export function buildPool(accountsPayload, creditsPayload) {
  const accounts = Array.isArray(accountsPayload?.accounts) ? accountsPayload.accounts : [];
  const credits =
    creditsPayload?.credits && typeof creditsPayload.credits === "object"
      ? creditsPayload.credits
      : {};
  return {
    total: accounts.length,
    accounts: accounts.map((item) => normalizeAccount(item, credits[item.id])),
  };
}

function creditText(credit) {
  if (!credit) return "积分未知";
  if (!credit.available) {
    return credit.error ? `积分查不了（${credit.error}）` : "积分查不了";
  }
  if (credit.credits === null) return "积分未知";
  const base =
    credit.total === null ? `${credit.credits}` : `${credit.credits}/${credit.total}`;
  const tail = credit.expirationText ? `（${credit.expirationText} 到期）` : "";
  const plan = credit.subscription ? `，${credit.subscription}` : "";
  return `积分 ${base}${tail}${plan}`;
}

function credentialText(account) {
  const label = CREDENTIAL_LABELS[account.credentialState] || account.credentialState;
  const bits = [label];
  const expires = dateOfIso(account.credentialExpiresAt);
  if (expires) bits.push(`${expires} 到期`);
  if (account.credentialRefreshable) bits.push("可自动续期");
  if (account.credentialDeviceForeign) bits.push("设备绑定在别处");
  return `凭据${bits.join(" · ")}`;
}

export function renderPool(value) {
  const lines = [`号池共 ${value.total} 个账号`];
  if (value.total === 0) {
    lines.push("（号池为空：先在 chaierww 里导入客户端当前登录的账号）");
  }
  for (const account of value.accounts) {
    const tags = [account.platform || account.scope || "未知平台"];
    if (account.group) tags.push(account.group);
    const parts = [
      account.id,
      account.displayName || "(未命名)",
      tags.join(" · "),
      creditText(account.credit),
      credentialText(account),
    ];
    if (account.note) parts.push(`备注：${account.note}`);
    lines.push(`- ${parts.join(" ｜ ")}`);
  }
  return lines.join("\n");
}

/* ---------- 用量 ---------- */

function normalizeUsage(raw) {
  return {
    accountId: text(raw.accountId),
    displayName: text(raw.displayName),
    platform: text(raw.platform),
    scope: text(raw.scope),
    detectable: Boolean(raw.detectable),
    requests: num(raw.requests),
    todayRequests: num(raw.todayRequests),
    todayCredit: num(raw.todayCredit),
    weekRequests: num(raw.weekRequests),
    weekCredit: num(raw.weekCredit),
    monthRequests: num(raw.monthRequests),
    monthCredit: num(raw.monthCredit),
    creditsConsumed:
      typeof raw.creditsConsumed === "number" ? raw.creditsConsumed : null,
    error: text(raw.error),
  };
}

export function buildUsage(payload) {
  const accounts = Array.isArray(payload?.accounts) ? payload.accounts : [];
  const normalized = accounts.map(normalizeUsage);
  return {
    total: normalized.length,
    detectableCount: normalized.filter((item) => item.detectable).length,
    accounts: normalized,
  };
}

export function renderUsage(value) {
  const lines = [`用量：${value.detectableCount}/${value.total} 个账号可查`];
  for (const account of value.accounts) {
    const who = `- ${account.displayName || account.accountId}（${
      account.platform || account.scope || "未知平台"
    }）`;
    if (!account.detectable) {
      lines.push(`${who}查不了：${account.error || "未知原因"}`);
      continue;
    }
    const parts = [];
    if (account.requests || account.todayRequests || account.monthRequests) {
      parts.push(`今日 ${account.todayRequests} 次 / ${account.todayCredit} 积分`);
      parts.push(`本月 ${account.monthRequests} 次 / ${account.monthCredit} 积分`);
    } else if (account.creditsConsumed !== null) {
      parts.push(`本月消耗 ${account.creditsConsumed} 积分`);
    } else {
      parts.push("暂无用量");
    }
    lines.push(`${who}${parts.join("；")}`);
  }
  return lines.join("\n");
}

/* ---------- 切换 ---------- */

export function buildSwitchReport(raw) {
  const plan = raw?.plan && typeof raw.plan === "object" ? raw.plan : null;
  return {
    state: text(raw?.state),
    dryRun: Boolean(raw?.dryRun),
    succeeded: Boolean(raw?.succeeded),
    scope: text(raw?.scope),
    accountId: text(raw?.accountId),
    snapshotId: text(raw?.snapshotId),
    error: text(raw?.error),
    rollbackFailed: Boolean(raw?.rollbackFailed),
    warnings: Array.isArray(raw?.warnings) ? raw.warnings.map(text).filter(Boolean) : [],
    steps: Array.isArray(raw?.steps)
      ? raw.steps.map((step) => ({
          step: text(step?.step),
          ok: Boolean(step?.ok),
          detail: text(step?.detail),
        }))
      : [],
    plan: plan
      ? {
          requiresAppExit: Boolean(plan.requiresAppExit),
          warnings: Array.isArray(plan.warnings)
            ? plan.warnings.map(text).filter(Boolean)
            : [],
          files: Array.isArray(plan.files)
            ? plan.files.map((file) => ({
                path: text(file?.path),
                action: text(file?.action),
                size: num(file?.size),
                note: text(file?.note),
              }))
            : [],
        }
      : null,
  };
}

const ACTION_LABELS = {
  write: "写入",
  create: "新建",
  copy: "复制",
  delete: "删除",
  remove: "删除",
  move: "移动",
  skip: "跳过",
};

export function renderSwitch(value) {
  const heading = value.dryRun
    ? `切换预演（${value.succeeded ? "可执行" : "存在风险"}）`
    : `切换${value.succeeded ? "完成" : "失败"}`;
  const lines = [
    `${heading}：账号 ${value.accountId}${value.scope ? `，作用域 ${value.scope}` : ""}`,
  ];
  if (value.snapshotId) lines.push(`备份快照：${value.snapshotId}`);
  if (value.plan) {
    lines.push(`需要先退出客户端：${value.plan.requiresAppExit ? "是" : "否"}`);
    const files = value.plan.files;
    lines.push(`计划改动 ${files.length} 个文件：`);
    for (const file of files.slice(0, 12)) {
      const action = ACTION_LABELS[file.action] || file.action || "改动";
      const size = formatSize(file.size);
      lines.push(`- ${action} ${file.path}${size ? `（${size}）` : ""}`);
    }
    if (files.length > 12) {
      lines.push(`- …还有 ${files.length - 12} 个文件未列出`);
    }
    for (const warning of value.plan.warnings) lines.push(`警告：${warning}`);
  }
  for (const warning of value.warnings) lines.push(`警告：${warning}`);
  if (value.error) lines.push(`错误：${value.error}`);
  if (value.rollbackFailed) {
    lines.push("回滚未成功：请到 chaierww 的备份页手动恢复");
  }
  if (!value.dryRun && value.steps.length > 0) {
    lines.push("步骤：");
    for (const step of value.steps) {
      lines.push(`- ${step.ok ? "完成" : "失败"}：${step.step}${step.detail ? `（${step.detail}）` : ""}`);
    }
  }
  return lines.join("\n");
}

/* ---------- 活动 ---------- */

export function buildActivitiesReport(raw) {
  const summary = raw?.summary && typeof raw.summary === "object" ? raw.summary : {};
  const results = Array.isArray(raw?.results) ? raw.results : [];
  return {
    summary: {
      total: num(summary.total),
      ok: num(summary.ok),
      skipped: num(summary.skipped),
      failed: num(summary.failed),
      retryable: num(summary.retryable),
      skips: Object.entries(summary)
        .filter(([key, value]) => key.startsWith("skip.") && num(value) > 0)
        .map(([key, value]) => ({ reason: key.slice(5), count: num(value) })),
    },
    results: results.map((item) => ({
      accountId: text(item?.accountId),
      displayName: text(item?.displayName),
      key: text(item?.key),
      title: text(item?.title),
      status: text(item?.status),
      message: text(item?.message),
      reason: text(item?.reason),
      reward: typeof item?.reward === "number" ? item.reward : null,
      retryable: Boolean(item?.retryable),
    })),
  };
}

export function renderActivities(value) {
  const summary = value.summary;
  const lines = [
    `活动执行：共 ${summary.total} 项，成功 ${summary.ok}，跳过 ${summary.skipped}，失败 ${summary.failed}`,
  ];
  const skips = summary.skips.map((item) => `${item.reason} ${item.count}`);
  if (skips.length > 0) lines.push(`跳过分类：${skips.join(" · ")}`);
  const failed = value.results.filter((item) => item.status === "failed");
  if (failed.length > 0) {
    lines.push("失败明细：");
    for (const item of failed.slice(0, 20)) {
      const who = item.displayName || item.accountId;
      lines.push(`- ${who} / ${item.title}：${item.message || item.reason || "未知原因"}`);
    }
    if (failed.length > 20) lines.push(`- …还有 ${failed.length - 20} 条未列出`);
  }
  if (summary.retryable > 0) lines.push(`其中可重试失败 ${summary.retryable} 项`);
  return lines.join("\n");
}

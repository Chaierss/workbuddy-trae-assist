/**
 * dsh-chaierww：把 chaierww 的号池、用量、切换与活动执行暴露成 DSH 工具。
 *
 * 设计取舍：
 * - host-only 组合包（没有 client）：DSH 里只需要「让模型能查、能切」，
 *   不需要再画一套界面；真正的界面仍在 chaierww 自己的 Web / 桌面端。
 * - 所有请求都发往本机回环地址，并带 `~/.chaierww/api-token` 的 Bearer 令牌；
 *   baseUrl 在插件加载时就校验，非回环地址直接拒绝启动。
 * - 写操作（切换 / 跑活动）必须显式传 `confirm: true`：宁可让模型多问一句，
 *   也不让它自己决定换掉用户正在用的账号。
 */

import { readFile } from "node:fs/promises";
import { homedir } from "node:os";

import Schema from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";

import {
  DEFAULT_BASE_URL,
  DEFAULT_TIMEOUT_MS,
  MAX_RESPONSE_BYTES,
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

export const name = "chaierww";
export const inject = ["tools"];

export const Config = Schema.object({
  /** chaierww 后端地址。只允许回环地址。 */
  baseUrl: Schema.string().default(DEFAULT_BASE_URL),
  /** 本机 API 令牌文件；留空 = `$CHAIERWW_HOME/api-token` 或 `~/.chaierww/api-token`。 */
  tokenFile: Schema.string().default(""),
  /** 单次请求超时（毫秒）。 */
  timeoutMs: Schema.number().default(DEFAULT_TIMEOUT_MS),
});

function queryOf(params = {}) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

function splitList(value) {
  if (typeof value !== "string") return undefined;
  const items = value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  return items.length > 0 ? items : undefined;
}

function requireConfirm(confirm, action) {
  if (confirm !== true) {
    throw new Error(
      `未执行${action}：必须显式传 confirm=true。这是写操作，只应在用户明确要求时调用。`,
    );
  }
}

function requireText(value, label, maxLength = 256) {
  const text = typeof value === "string" ? value.trim() : "";
  if (!text) throw new Error(`${label} 不能为空`);
  if (text.length > maxLength) throw new Error(`${label} 过长（上限 ${maxLength} 字符）`);
  return text;
}

export function apply(ctx, config) {
  const baseUrl = normalizeBaseUrl(config.baseUrl);
  const tokenPath = resolveTokenPath({
    tokenFile: config.tokenFile,
    env: process.env,
    home: homedir(),
  });
  const timeoutMs =
    Number.isFinite(config.timeoutMs) && config.timeoutMs > 0
      ? config.timeoutMs
      : DEFAULT_TIMEOUT_MS;

  async function readToken() {
    let raw;
    try {
      raw = await readFile(tokenPath, "utf8");
    } catch (error) {
      if (error?.code === "ENOENT") {
        throw new Error(
          `找不到本机 API 令牌（${tokenPath}）：请先启动一次 chaierww，再重试。`,
        );
      }
      throw new Error(`读取本机 API 令牌失败（${tokenPath}）：${error?.message || "未知错误"}`);
    }
    const token = raw.trim();
    if (token.length < 32) {
      throw new Error(`本机 API 令牌格式异常（${tokenPath}）：请删除该文件后重启 chaierww 重新生成。`);
    }
    return token;
  }

  async function call(pathname, { method = "GET", query, body, signal } = {}) {
    const token = await readToken();
    const url = `${baseUrl}${pathname}${queryOf(query)}`;
    const headers = {
      Authorization: `Bearer ${token}`,
      "X-Chaierww-Client": "web",
      Accept: "application/json",
      "Cache-Control": "no-store",
    };
    if (body !== undefined) headers["Content-Type"] = "application/json";

    const timeout = AbortSignal.timeout(timeoutMs);
    const signals = signal ? [signal, timeout] : [timeout];
    let response;
    try {
      response = await fetch(url, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.any(signals),
        redirect: "error",
      });
    } catch (error) {
      if (error?.name === "TimeoutError" || error?.name === "AbortError") {
        throw new Error(`请求 chaierww 超时或被取消（${timeoutMs} ms）`);
      }
      const reason = error?.cause?.code || error?.message || "未知错误";
      throw new Error(
        `连不上 chaierww（${baseUrl}）：${reason}。请先启动 chaierww，或检查 baseUrl / CHAIERWW_PORT。`,
      );
    }

    const declared = Number(response.headers.get("content-length") || 0);
    if (declared > MAX_RESPONSE_BYTES) {
      throw new Error(`chaierww 响应过大（${declared} 字节），已拒绝解析`);
    }
    const raw = await response.text();
    if (raw.length > MAX_RESPONSE_BYTES) {
      throw new Error("chaierww 响应过大，已拒绝解析");
    }

    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`.trim();
      try {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed.detail === "string" && parsed.detail) {
          detail = `${response.status} ${parsed.detail}`;
        }
      } catch {
        // 上游不是 JSON：保留状态码即可，不把响应正文塞进错误里。
      }
      throw new Error(`chaierww 接口失败：${detail}`);
    }
    if (!raw) return {};
    try {
      return JSON.parse(raw);
    } catch {
      throw new Error("chaierww 返回的不是有效 JSON");
    }
  }

  ctx.tools.register(
    defineTool({
      name: "chaierww_pool",
      description:
        "列出 chaierww 号池里的 WorkBuddy / Trae 账号：分组、备注、凭据状态与积分余额。只读，不改任何文件。前置条件：本机 chaierww 正在运行（默认 127.0.0.1:57910）。失败语义：连不上、令牌缺失或接口报错会抛异常并说明原因，不会返回空号池。",
      parameters: {
        refresh: {
          type: "boolean",
          description: "是否绕过积分缓存、重新向上游查一次余额；默认 false",
        },
        query: {
          type: "string",
          description: "按账号名 / 备注 / 分组过滤；省略 = 全部账号",
        },
      },
      output: {
        schema: {
          type: "object",
          properties: {
            total: { type: "number" },
            accounts: {
              type: "array",
              items: {
                type: "object",
                properties: {
                  id: { type: "string" },
                  displayName: { type: "string" },
                  platform: { type: "string" },
                  scope: { type: "string" },
                  group: { type: "string" },
                  note: { type: "string" },
                  credentialState: { type: "string" },
                  credentialExpiresAt: { type: "string" },
                  credentialRefreshable: { type: "boolean" },
                  credentialDeviceForeign: { type: "boolean" },
                  credit: {
                    oneOf: [
                      {
                        type: "object",
                        properties: {
                          available: { type: "boolean" },
                          credits: {
                            oneOf: [{ type: "number" }, { type: "null" }],
                          },
                          total: {
                            oneOf: [{ type: "number" }, { type: "null" }],
                          },
                          expiresAt: {
                            oneOf: [{ type: "number" }, { type: "null" }],
                          },
                          expirationText: { type: "string" },
                          subscription: { type: "string" },
                          error: { type: "string" },
                        },
                        additionalProperties: false,
                      },
                      { type: "null" },
                    ],
                  },
                },
                additionalProperties: false,
              },
            },
          },
          additionalProperties: false,
        },
        render: (_args, value) => [{ type: "text", text: renderPool(value) }],
      },
      async execute(args, exec) {
        const signal = exec?.signal;
        const [accounts, credits] = await Promise.all([
          call("/api/accounts/merged", { query: { q: args.query }, signal }),
          call("/api/accounts/credits", {
            query: { refresh: Boolean(args.refresh) },
            signal,
          }),
        ]);
        return buildPool(accounts, credits);
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "chaierww_usage",
      description:
        "查询 chaierww 号池里各账号的用量：今日 / 本周 / 本月的请求数与积分消耗。只读，不发上游写请求。前置条件：本机 chaierww 正在运行。detectable=false 表示该账号查不了用量（不等于没用过），渲染结果会写明原因。",
      parameters: {
        refresh: {
          type: "boolean",
          description: "是否绕过缓存重新向上游查询；默认 false",
        },
        accountIds: {
          type: "string",
          description: "逗号分隔的账号 id；省略 = 号池全部账号",
        },
      },
      output: {
        schema: {
          type: "object",
          properties: {
            total: { type: "number" },
            detectableCount: { type: "number" },
            accounts: {
              type: "array",
              items: {
                type: "object",
                properties: {
                  accountId: { type: "string" },
                  displayName: { type: "string" },
                  platform: { type: "string" },
                  scope: { type: "string" },
                  detectable: { type: "boolean" },
                  requests: { type: "number" },
                  todayRequests: { type: "number" },
                  todayCredit: { type: "number" },
                  weekRequests: { type: "number" },
                  weekCredit: { type: "number" },
                  monthRequests: { type: "number" },
                  monthCredit: { type: "number" },
                  creditsConsumed: {
                    oneOf: [{ type: "number" }, { type: "null" }],
                  },
                  error: { type: "string" },
                },
                additionalProperties: false,
              },
            },
          },
          additionalProperties: false,
        },
        render: (_args, value) => [{ type: "text", text: renderUsage(value) }],
      },
      async execute(args, exec) {
        const ids = splitList(args.accountIds);
        const payload = await call("/api/usage", {
          query: {
            refresh: Boolean(args.refresh),
            accountIds: ids ? ids.join(",") : undefined,
          },
          signal: exec?.signal,
        });
        return buildUsage(payload);
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "chaierww_switch",
      description:
        "把本机客户端切换到号池中的某个账号。默认只预演（dryRun=true），真正写入必须显式传 confirm=true；只应在用户明确要求换号时调用。副作用：写客户端登录态文件、切换前自动备份、失败自动回滚。前置条件：账号 id 来自 chaierww_pool；被切换的客户端需要先退出（预演结果里会说明）。",
      parameters: {
        accountId: {
          type: "string",
          required: true,
          description: "号池账号 id（先用 chaierww_pool 查询）",
        },
        confirm: {
          type: "boolean",
          required: true,
          description: "必须为 true 才执行；false 或不传会直接拒绝",
        },
        dryRun: {
          type: "boolean",
          description: "true = 只返回切换计划，不写任何文件；默认 false",
        },
        relaunch: {
          type: "boolean",
          description: "切换后是否自动拉起客户端；默认 false",
        },
        slot: {
          type: "string",
          description: "程序位 key（多宿主客户端）；省略 = 该平台默认位",
        },
      },
      output: {
        schema: {
          type: "object",
          properties: {
            state: { type: "string" },
            dryRun: { type: "boolean" },
            succeeded: { type: "boolean" },
            scope: { type: "string" },
            accountId: { type: "string" },
            snapshotId: { type: "string" },
            error: { type: "string" },
            rollbackFailed: { type: "boolean" },
            warnings: { type: "array", items: { type: "string" } },
            steps: {
              type: "array",
              items: {
                type: "object",
                properties: {
                  step: { type: "string" },
                  ok: { type: "boolean" },
                  detail: { type: "string" },
                },
                additionalProperties: false,
              },
            },
            plan: {
              oneOf: [
                {
                  type: "object",
                  properties: {
                    requiresAppExit: { type: "boolean" },
                    warnings: { type: "array", items: { type: "string" } },
                    files: {
                      type: "array",
                      items: {
                        type: "object",
                        properties: {
                          path: { type: "string" },
                          action: { type: "string" },
                          size: { type: "number" },
                          note: { type: "string" },
                        },
                        additionalProperties: false,
                      },
                    },
                  },
                  additionalProperties: false,
                },
                { type: "null" },
              ],
            },
          },
          additionalProperties: false,
        },
        render: (_args, value) => [{ type: "text", text: renderSwitch(value) }],
      },
      async execute(args, exec) {
        requireConfirm(args.confirm, "切换账号");
        const accountId = requireText(args.accountId, "accountId");
        const dryRun = Boolean(args.dryRun);
        const payload = await call(dryRun ? "/api/switch/dry-run" : "/api/switch", {
          method: "POST",
          body: {
            accountId,
            relaunch: Boolean(args.relaunch),
            slot: args.slot ? requireText(args.slot, "slot") : null,
          },
          signal: exec?.signal,
        });
        return buildSwitchReport(payload);
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "chaierww_activities_run",
      description:
        "对号池账号执行可自动完成的每日签到与官方活动（已完成的自动跳过，需手动操作的项不会代做）。副作用：向上游发请求并领取奖励，可能改变账号活动进度；必须显式传 confirm=true，且只应在用户明确要求时调用。前置条件：本机 chaierww 正在运行；同一时间只能有一次执行。",
      parameters: {
        confirm: {
          type: "boolean",
          required: true,
          description: "必须为 true 才执行；false 或不传会直接拒绝",
        },
        accountIds: {
          type: "string",
          description: "逗号分隔的账号 id；省略 = 号池全部可检测账号",
        },
        keys: {
          type: "string",
          description: "逗号分隔的活动 key（只跑这些）；省略 = 当前全部可用活动",
        },
      },
      output: {
        schema: {
          type: "object",
          properties: {
            summary: {
              type: "object",
              properties: {
                total: { type: "number" },
                ok: { type: "number" },
                skipped: { type: "number" },
                failed: { type: "number" },
                retryable: { type: "number" },
                skips: {
                  type: "array",
                  items: {
                    type: "object",
                    properties: {
                      reason: { type: "string" },
                      count: { type: "number" },
                    },
                    additionalProperties: false,
                  },
                },
              },
              additionalProperties: false,
            },
            results: {
              type: "array",
              items: {
                type: "object",
                properties: {
                  accountId: { type: "string" },
                  displayName: { type: "string" },
                  key: { type: "string" },
                  title: { type: "string" },
                  status: { type: "string" },
                  message: { type: "string" },
                  reason: { type: "string" },
                  reward: {
                    oneOf: [{ type: "number" }, { type: "null" }],
                  },
                  retryable: { type: "boolean" },
                },
                additionalProperties: false,
              },
            },
          },
          additionalProperties: false,
        },
        render: (_args, value) => [{ type: "text", text: renderActivities(value) }],
      },
      async execute(args, exec) {
        requireConfirm(args.confirm, "执行活动");
        const accountIds = splitList(args.accountIds);
        const keys = splitList(args.keys);
        const payload = await call("/api/activities/run", {
          method: "POST",
          body: {
            accountIds: accountIds ?? null,
            keys: keys ?? null,
          },
          signal: exec?.signal,
        });
        return buildActivitiesReport(payload);
      },
    }),
  );
}

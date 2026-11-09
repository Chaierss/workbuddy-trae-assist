// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { t } from "./lib/i18n";

/**
 * 错误可读性守卫（1.3.0）：所有 HTTP 失败都得把请求编号带回前端，
 * 用户报错时才能和 ~/.chaierww/logs/ 里同一次请求对上。
 */
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function requestIdOf(headers: HeadersInit | undefined): string {
  return (headers as Record<string, string> | undefined)?.["X-Request-Id"] ?? "";
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api.request 错误信息", () => {
  it("5xx + detail：消息包含 detail 与同一个请求编号", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: "账号不存在" }, 500));
    vi.stubGlobal("fetch", fetchMock);

    const error = await api.health().catch((caught: Error) => caught);
    const sentId = requestIdOf(fetchMock.mock.calls[0]?.[1]?.headers);

    expect(error).toBeInstanceOf(Error);
    expect((error as Error).message).toContain("账号不存在");
    expect((error as Error).message).toContain(t("api.requestId", { id: sentId }));
  });

  it("5xx + 非 JSON 文本：消息包含原文与请求编号", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response("gateway exploded", { status: 502 }));
    vi.stubGlobal("fetch", fetchMock);

    const error = await api.health().catch((caught: Error) => caught);
    const sentId = requestIdOf(fetchMock.mock.calls[0]?.[1]?.headers);

    expect((error as Error).message).toContain("gateway exploded");
    expect((error as Error).message).toContain(t("api.requestId", { id: sentId }));
  });

  it("连不上后端：抛出网络错误文案", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    await expect(api.health()).rejects.toThrow(t("api.networkError"));
  });

  it("2xx JSON：原样返回数据", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ status: "ok" })));

    await expect(api.health()).resolves.toEqual({ status: "ok" });
  });
});

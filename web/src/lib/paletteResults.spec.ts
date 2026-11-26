import { describe, expect, it, vi } from "vitest";
import type { Account } from "../api";
import type { PaletteAction } from "./paletteResults";
import type { NavDestination } from "./nav";

// i18n 在模块加载时会读 localStorage；Node 环境没有它，先补一个空实现。
vi.stubGlobal("localStorage", {
  getItem: () => null,
  setItem: () => undefined,
  removeItem: () => undefined,
});
vi.stubGlobal("document", {
  documentElement: {},
  createElement: () => ({ content: {} }),
  createElementNS: () => ({}),
  querySelector: () => null,
});
const { matchPalette } = await import("./paletteResults");

const icon = {} as NavDestination["icon"];

const nav: NavDestination[] = [
  { key: "accounts", labelKey: "nav.accounts", path: "/accounts", icon },
  { key: "activity", labelKey: "nav.activity", path: "/activity", icon },
];

function account(id: string, name: string): Account {
  return {
    id,
    scope: "workbuddy-cn",
    platform: "workbuddy",
    externalId: `u-${id}`,
    displayName: name,
    note: "",
    credentialState: "usable",
  } as unknown as Account;
}

const action: PaletteAction = {
  key: "login-probe",
  label: "复检登录态",
  hint: "只读探测，不续期",
  icon,
  run: async () => undefined,
};

describe("matchPalette", () => {
  it("空关键词只列页面，不把动作和账号倒出来", () => {
    const results = matchPalette("", nav, [account("1", "甲")], [action]);
    expect(results.map((item) => item.kind)).toEqual(["nav", "nav"]);
  });

  it("关键词能命中动作 key 与中文标签", () => {
    expect(matchPalette("login-probe", nav, [], [action])[0]?.kind).toBe("action");
    expect(matchPalette("复检", nav, [], [action])[0]?.kind).toBe("action");
  });

  it("账号最多 6 条，避免面板变成第二张号池表", () => {
    const accounts = Array.from({ length: 10 }, (_, index) =>
      account(String(index), `测试号 ${index}`),
    );
    const results = matchPalette("测试", nav, accounts, []);
    expect(results.filter((item) => item.kind === "account")).toHaveLength(6);
  });

  it("账号可按平台与备注匹配", () => {
    const target = account("1", "甲");
    target.note = "备用";
    expect(matchPalette("备用", nav, [target], [])[0]?.kind).toBe("account");
    expect(matchPalette("workbuddy", nav, [target], [])[0]?.kind).toBe("account");
  });
});

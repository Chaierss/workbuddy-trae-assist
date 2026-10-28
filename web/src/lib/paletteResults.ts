import type { Component } from "vue";
import type { Account } from "../api";
import { t } from "./i18n";
import type { NavDestination } from "./nav";

/** 命令面板的一行：页面跳转，或一个可直接换号的账号。 */
export type PaletteResult =
  | {
      kind: "nav";
      key: string;
      label: string;
      path: string;
      icon: Component;
    }
  | {
      kind: "account";
      key: string;
      label: string;
      hint: string;
      account: Account;
    };

const ACCOUNT_LIMIT = 6;

function platformLabel(platform: string): string {
  if (platform === "trae") return "Trae";
  if (platform === "workbuddy") return "WorkBuddy";
  return platform;
}

function stateLabel(state: Account["credentialState"]): string {
  if (state === "relogin_required") return t("accounts.state.relogin");
  if (state === "unknown") return t("accounts.state.unknown");
  if (state === "unverified") return t("palette.state.unverified");
  return t("palette.state.usable");
}

function accountHint(account: Account): string {
  return `${platformLabel(account.platform)} · ${stateLabel(account.credentialState)}`;
}

/**
 * 面板结果：空关键词只列页面；有输入时页面与账号一起匹配。
 * 纯函数，账号最多 `ACCOUNT_LIMIT` 条，避免面板变成第二张号池表。
 */
export function matchPalette(
  keyword: string,
  navItems: NavDestination[],
  accounts: Account[],
): PaletteResult[] {
  const query = keyword.trim().toLowerCase();
  const nav = navItems
    .filter(
      (item) =>
        !query ||
        t(item.labelKey).toLowerCase().includes(query) ||
        item.key.includes(query) ||
        item.path.includes(query),
    )
    .map<PaletteResult>((item) => ({
      kind: "nav",
      key: `nav:${item.key}`,
      label: t(item.labelKey),
      path: item.path,
      icon: item.icon,
    }));

  if (!query) return nav;

  const matchedAccounts = accounts
    .filter(
      (account) =>
        account.displayName.toLowerCase().includes(query) ||
        account.note.toLowerCase().includes(query) ||
        account.externalId.toLowerCase().includes(query) ||
        account.platform.toLowerCase().includes(query) ||
        platformLabel(account.platform).toLowerCase().includes(query),
    )
    .slice(0, ACCOUNT_LIMIT)
    .map<PaletteResult>((account) => ({
      kind: "account",
      key: `account:${account.id}`,
      label: account.displayName,
      hint: accountHint(account),
      account,
    }));

  return [...nav, ...matchedAccounts];
}

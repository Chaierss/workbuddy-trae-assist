import type { Component } from "vue";
import type { Account } from "../api";
import { t } from "./i18n";
import type { NavDestination } from "./nav";

/** 命令面板里可以直接执行的动作（与页面跳转、换号分开）。 */
export interface PaletteAction {
  key: string;
  label: string;
  hint: string;
  icon: Component;
  /** 会写上游 / 产生数据的动作，执行前要二次确认。 */
  destructive?: boolean;
  /** 执行体；结果由面板统一提示并写入任务中心。 */
  run: () => Promise<unknown>;
  successMessage?: string;
}

/** 命令面板的一行：页面跳转、可执行动作，或一个可直接换号的账号。 */
export type PaletteResult =
  | {
      kind: "nav";
      key: string;
      label: string;
      path: string;
      icon: Component;
      /** 来自「最近使用」，只影响分组，不影响行为。 */
      recent?: boolean;
    }
  | {
      kind: "action";
      key: string;
      label: string;
      hint: string;
      icon: Component;
      action: PaletteAction;
      recent?: boolean;
    }
  | {
      kind: "account";
      key: string;
      label: string;
      hint: string;
      account: Account;
      recent?: boolean;
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

/** 一行参与检索的文本：主标题 + 补充词（英文名 / 拼音 / 首字母）。 */
interface Searchable {
  label: string;
  keywords?: string[];
  extra?: string[];
}

/**
 * 给一行打分：`score` 越小越靠前，`missing` 是没命中的词数。
 *
 * - 空格分词：所有词都命中的行排在最前；
 * - 主标题命中优于补充词命中，完全相等 > 前缀 > 包含；
 * - 一个词都没命中（missing === 词数）视为无关，直接丢弃。
 */
function score(item: Searchable, tokens: string[]): { score: number; missing: number } {
  const label = item.label.toLowerCase();
  const words = [
    label,
    ...(item.keywords ?? []).map((word) => word.toLowerCase()),
    ...(item.extra ?? []).map((word) => word.toLowerCase()),
  ];
  let total = 0;
  let missing = 0;
  for (const token of tokens) {
    let best = Infinity;
    for (let index = 0; index < words.length; index += 1) {
      const word = words[index];
      if (word === token) best = Math.min(best, index === 0 ? 0 : 3);
      else if (word.startsWith(token)) best = Math.min(best, index === 0 ? 1 : 4);
      else if (word.includes(token)) best = Math.min(best, index === 0 ? 2 : 5);
    }
    if (Number.isFinite(best)) total += best;
    else missing += 1;
  }
  return { score: total, missing };
}

/**
 * 按匹配质量排序；同分保持登记顺序（稳定）。
 *
 * `strict` 为 true 时只保留每个词都命中的行；否则允许部分命中，
 * 先按「少漏词」再按分数排 —— 这样输入两个词时不至于直接掉进兜底建议。
 */
function rank<T>(
  items: T[],
  tokens: string[],
  searchable: (item: T) => Searchable,
  strict: boolean,
): T[] {
  return items
    .map((item) => ({ item, ...score(searchable(item), tokens) }))
    .filter((entry) => (strict ? entry.missing === 0 : entry.missing < tokens.length))
    .sort((a, b) => (strict ? a.score - b.score : a.missing - b.missing || a.score - b.score))
    .map((entry) => entry.item);
}

/**
 * 面板结果：空关键词只列页面；有输入时页面、动作与账号一起匹配。
 * 纯函数，账号最多 `ACCOUNT_LIMIT` 条，避免面板变成第二张号池表。
 */
export function matchPalette(
  keyword: string,
  navItems: NavDestination[],
  accounts: Account[],
  actions: PaletteAction[] = [],
): PaletteResult[] {
  const query = keyword.trim().toLowerCase();
  const tokens = query.split(/\s+/).filter(Boolean);
  const searching = tokens.length > 0;

  const navOf = (item: NavDestination): Searchable => ({
    label: t(item.labelKey),
    keywords: item.keywords,
    extra: [item.key, item.path],
  });
  const actionOf = (action: PaletteAction): Searchable => ({
    label: action.label,
    keywords: [action.key],
    extra: [action.hint],
  });
  const accountOf = (account: Account): Searchable => ({
    label: account.displayName,
    keywords: [
      account.externalId,
      account.platform,
      platformLabel(account.platform),
      account.note,
    ],
  });

  const navMatched = !searching
    ? navItems
    : (() => {
        const strict = rank(navItems, tokens, navOf, true);
        return strict.length ? strict : rank(navItems, tokens, navOf, false);
      })();

  const matchedNav = navMatched.map<PaletteResult>((item) => ({
    kind: "nav",
    key: `nav:${item.key}`,
    label: t(item.labelKey),
    path: item.path,
    icon: item.icon,
  }));

  if (!searching) return matchedNav;

  const actionStrict = rank(actions, tokens, actionOf, true);
  const matchedActions = (actionStrict.length
    ? actionStrict
    : rank(actions, tokens, actionOf, false)
  ).map<PaletteResult>((action) => ({
    kind: "action",
    key: `action:${action.key}`,
    label: action.label,
    hint: action.hint,
    icon: action.icon,
    action,
  }));

  const accountStrict = rank(accounts, tokens, accountOf, true);
  const matchedAccounts = (accountStrict.length
    ? accountStrict
    : rank(accounts, tokens, accountOf, false)
  )
    .slice(0, ACCOUNT_LIMIT)
    .map<PaletteResult>((account) => ({
      kind: "account",
      key: `account:${account.id}`,
      label: account.displayName,
      hint: accountHint(account),
      account,
    }));

  return [...matchedNav, ...matchedActions, ...matchedAccounts];
}

import type { Component } from "vue";
import {
  BookOpen,
  CalendarClock,
  Database,
  FileCog,
  Palette,
  Repeat2,
} from "lucide-vue-next";
import type { MessageKey } from "../../lib/i18n";
import { isDesktop } from "../../lib/desktop";
import AppearanceTab from "./AppearanceTab.vue";
import AccountSwitchTab from "./AccountSwitchTab.vue";
import AutomationTab from "./AutomationTab.vue";
import DataTab from "./DataTab.vue";
import ClientEnvTab from "./ClientEnvTab.vue";
import DocsTab from "./DocsTab.vue";

/**
 * 设置 tab 的唯一登记处。加一个 tab 只改这里。
 *
 * 1.13.0 把 11 个分栏收敛到 6 个：外观与语言、账号与切换、自动化、数据与备份、
 * 客户端与环境、规则与文档。收敛原则是「同一个能力只有一个可写入口」——
 * 计划任务与自动处置、迁移与同步、客户端与环境，原先各拆两栏，实际使用时总是一起看。
 *
 * 旧地址（?tab=general / schedule / sync / env / backup / rules / components / api / wiki / logs）
 * 继续可用，由 `LEGACY_TAB_ALIASES` 映射到新分栏，避免收藏夹和后台通知里的深链失效。
 *
 * `permission` 字段暂时留空：本项目还没有 RBAC 体系。
 * 真加权限时规则是「容器不设 permission、权限下沉到每个 tab」，
 * 否则只有文档权限的人会被挡在设置外面 —— 而接口文档与 Wiki 是通用能力。
 */
export interface SettingsTab {
  key: string;
  labelKey: MessageKey;
  hintKey: MessageKey;
  icon: Component;
  component: Component;
  /** 页面自己管滚动（iframe / 双栏文档），容器不再套一层滚动与内边距。 */
  selfScroll?: boolean;
  /**
   * 桌面端隐藏。规则 / 组件 / 接口文档 / Wiki 属于「开发与浏览场景」页面，
   * 桌面壳里既没有地址栏也没有多标签页；需要时到浏览器打开。
   */
  hideOnDesktop?: boolean;
}

export const SETTINGS_ROUTE = "/settings";
export const SETTINGS_DEFAULT_KEY = "appearance";

export const SETTINGS_TABS: SettingsTab[] = [
  {
    key: "appearance",
    labelKey: "settings.appearance.label",
    hintKey: "settings.appearance.hint",
    icon: Palette,
    component: AppearanceTab,
  },
  {
    key: "accounts",
    labelKey: "settings.accounts.label",
    hintKey: "settings.accounts.hint",
    icon: Repeat2,
    component: AccountSwitchTab,
  },
  {
    key: "automation",
    labelKey: "settings.automation.label",
    hintKey: "settings.automation.hint",
    icon: CalendarClock,
    component: AutomationTab,
  },
  {
    key: "data",
    labelKey: "settings.data.label",
    hintKey: "settings.data.hint",
    icon: Database,
    component: DataTab,
  },
  {
    key: "client",
    labelKey: "settings.clientEnv.label",
    hintKey: "settings.clientEnv.hint",
    icon: FileCog,
    component: ClientEnvTab,
  },
  {
    key: "docs",
    labelKey: "settings.docs.label",
    hintKey: "settings.docs.hint",
    icon: BookOpen,
    component: DocsTab,
    selfScroll: true,
    hideOnDesktop: true,
  },
];

/** 旧分栏名 → 新分栏；发过的深链与后台通知继续有效。 */
const LEGACY_TAB_ALIASES: Record<string, string> = {
  general: "appearance",
  schedule: "automation",
  sync: "accounts",
  env: "client",
  backup: "data",
  logs: "data",
  rules: "docs",
  components: "docs",
  api: "docs",
  wiki: "docs",
};

function canonicalKey(raw: string): string {
  return LEGACY_TAB_ALIASES[raw] ?? raw;
}

/**
 * 当前环境可见的分栏。左栏、内容区、`?tab=` 归一必须共用这一份，
 * 否则会出现「左栏没有这一项，但地址栏进去是空白」。
 */
export function visibleSettingsTabs(desktopMode: boolean = isDesktop): SettingsTab[] {
  return SETTINGS_TABS.filter((tab) => !(tab.hideOnDesktop && desktopMode));
}

/** 在「可见分栏」里查，所以桌面端查不到 docs。 */
export function findSettingsTab(key: unknown, desktopMode: boolean = isDesktop): SettingsTab | null {
  if (typeof key !== "string") return null;
  const canonical = canonicalKey(key);
  return visibleSettingsTabs(desktopMode).find((tab) => tab.key === canonical) ?? null;
}

/** 激活项解析顺序：合法 ?tab=（含旧名）→ 默认项 → 第一个可见项。 */
export function resolveSettingsKey(raw: unknown, desktopMode: boolean = isDesktop): string {
  const tabs = visibleSettingsTabs(desktopMode);
  const matched = findSettingsTab(raw, desktopMode);
  if (matched) return matched.key;
  const fallback = tabs.find((tab) => tab.key === SETTINGS_DEFAULT_KEY);
  return fallback?.key ?? tabs[0]?.key ?? "";
}

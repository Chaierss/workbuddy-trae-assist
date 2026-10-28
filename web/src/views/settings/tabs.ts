import type { Component } from "vue";
import {
  BookOpen,
  Boxes,
  CalendarClock,
  Database,
  FileCog,
  Layers,
  ListChecks,
  Plug,
  ScrollText,
  Settings2,
  SlidersHorizontal,
} from "lucide-vue-next";
import type { MessageKey } from "../../lib/i18n";
import { isDesktop } from "../../lib/desktop";
import GeneralTab from "./GeneralTab.vue";
import ClientSettingsTab from "./ClientSettingsTab.vue";
import ScheduleTab from "./ScheduleTab.vue";
import EnvironmentTab from "./EnvironmentTab.vue";
import BackupTab from "./BackupTab.vue";
import ApiDocsTab from "./ApiDocsTab.vue";
import WikiTab from "./WikiTab.vue";
import LogsTab from "./LogsTab.vue";
import RulesTab from "./RulesTab.vue";
import ComponentsTab from "./ComponentsTab.vue";
import SyncTab from "./SyncTab.vue";

/**
 * 设置 tab 的唯一登记处。加一个 tab 只改这里。
 *
 * `permission` 字段暂时留空：本项目还没有 RBAC 体系。
 * 真加权限时规则是「容器不设 permission、权限下沉到每个 tab」，
 * 否则只有文档权限的人会被挡在设置外面 —— 而接口文档与 Wiki 是通用能力。
 *
 * 「版本更新」不在这里：它是桌面端独有的能力，已经并进「常规」页的
 * `UpdateSection`，不再单独占一个分栏。
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
   * 桌面端隐藏。接口文档 / Wiki / 规则 / 组件都属于「开发与浏览场景」页面：
   * 前两个是同源 iframe 套一层文档，在桌面壳里既没有地址栏也没有多标签页；
   * 后两个面向开发维护，普通使用者不需要。桌面端一律不展示，需要时到浏览器打开。
   */
  hideOnDesktop?: boolean;
}

export const SETTINGS_ROUTE = "/settings";
export const SETTINGS_DEFAULT_KEY = "general";

export const SETTINGS_TABS: SettingsTab[] = [
  {
    key: "general",
    labelKey: "settings.general.label",
    hintKey: "settings.general.hint",
    icon: Settings2,
    component: GeneralTab,
  },
  {
    key: "schedule",
    labelKey: "settings.schedule.label",
    hintKey: "settings.schedule.hint",
    icon: CalendarClock,
    component: ScheduleTab,
  },
  {
    key: "client",
    labelKey: "settings.client.label",
    hintKey: "settings.client.hint",
    icon: FileCog,
    component: ClientSettingsTab,
  },
  {
    key: "sync",
    labelKey: "settings.sync.label",
    hintKey: "settings.sync.hint",
    icon: Layers,
    component: SyncTab,
  },
  {
    key: "env",
    labelKey: "settings.env.label",
    hintKey: "settings.env.hint",
    icon: SlidersHorizontal,
    component: EnvironmentTab,
  },
  {
    key: "backup",
    labelKey: "settings.backup.label",
    hintKey: "settings.backup.hint",
    icon: Database,
    component: BackupTab,
  },
  {
    key: "rules",
    labelKey: "settings.rules.label",
    hintKey: "settings.rules.hint",
    icon: ListChecks,
    component: RulesTab,
    hideOnDesktop: true,
  },
  {
    key: "components",
    labelKey: "settings.components.label",
    hintKey: "settings.components.hint",
    icon: Boxes,
    component: ComponentsTab,
    hideOnDesktop: true,
  },
  {
    key: "api",
    labelKey: "settings.api.label",
    hintKey: "settings.api.hint",
    icon: Plug,
    component: ApiDocsTab,
    selfScroll: true,
    hideOnDesktop: true,
  },
  {
    key: "wiki",
    labelKey: "settings.wiki.label",
    hintKey: "settings.wiki.hint",
    icon: BookOpen,
    component: WikiTab,
    selfScroll: true,
    hideOnDesktop: true,
  },
  {
    key: "logs",
    labelKey: "settings.logs.label",
    hintKey: "settings.logs.hint",
    icon: ScrollText,
    component: LogsTab,
  },
];

/**
 * 当前环境可见的分栏。左栏、内容区、`?tab=` 归一必须共用这一份，
 * 否则会出现「左栏没有这一项，但地址栏进去是空白」。
 */
export function visibleSettingsTabs(desktopMode: boolean = isDesktop): SettingsTab[] {
  return SETTINGS_TABS.filter((tab) => !(tab.hideOnDesktop && desktopMode));
}

/** 在「可见分栏」里查，所以桌面端查不到 api / wiki。 */
export function findSettingsTab(key: unknown, desktopMode: boolean = isDesktop): SettingsTab | null {
  if (typeof key !== "string") return null;
  return visibleSettingsTabs(desktopMode).find((tab) => tab.key === key) ?? null;
}

/** 激活项解析顺序：合法 ?tab= → 默认项 → 第一个可见项。 */
export function resolveSettingsKey(raw: unknown, desktopMode: boolean = isDesktop): string {
  const tabs = visibleSettingsTabs(desktopMode);
  const matched = findSettingsTab(raw, desktopMode);
  if (matched) return matched.key;
  const fallback = tabs.find((tab) => tab.key === SETTINGS_DEFAULT_KEY);
  return fallback?.key ?? tabs[0]?.key ?? "";
}

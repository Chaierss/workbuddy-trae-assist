import type { Component } from "vue";
import {
  Activity,
  HeartPulse,
  LayoutDashboard,
  Network,
  Settings,
  Sparkles,
  Users,
} from "lucide-vue-next";
import type { MessageKey } from "./i18n";

/**
 * 主导航目的地的唯一登记处：侧栏、命令面板、快捷键都读这一份，
 * 避免「页面清单 + 文案」在三处各写一遍、改一处漏两处。
 *
 * 这里存的是**词条 key**而不是译文：模块级常量只求值一次，放 `t()` 会导致切语言不更新。
 */
export interface NavDestination {
  key: string;
  labelKey: MessageKey;
  /** 不带作用域前缀的短路径；实际跳转地址由 `scopedPath()` 补前缀。 */
  path: string;
  icon: Component;
  /** 侧栏分组：工作台 / 运营 / 运维。命令面板不需要，故为可选。 */
  section?: NavSection;
  /**
   * 命令面板的补充检索词：英文名、拼音全拼、拼音首字母。
   * 中文用户习惯敲 `hc` 找号池、敲 `sz` 找设置，只靠 label 的子串匹配找不到。
   */
  keywords?: string[];
}

export type NavSection = "workbench" | "ops" | "infra";

export const NAV_DESTINATIONS: NavDestination[] = [
  {
    key: "overview",
    labelKey: "nav.overview",
    path: "/overview",
    icon: LayoutDashboard,
    section: "workbench",
    keywords: ["overview", "workbench", "home", "gongzuotai", "gzt"],
  },
  {
    key: "accounts",
    labelKey: "nav.accounts",
    path: "/accounts",
    icon: Users,
    section: "workbench",
    keywords: ["accounts", "account", "pool", "haochi", "hc"],
  },
  {
    key: "activity",
    labelKey: "nav.activity",
    path: "/activity",
    icon: Sparkles,
    section: "ops",
    keywords: ["activity", "activities", "campaign", "huodong", "hd"],
  },
  {
    key: "usage",
    labelKey: "nav.usage",
    path: "/usage",
    icon: Activity,
    section: "ops",
    keywords: ["usage", "stats", "yongliang", "yl"],
  },
  // 健康中心跨平台（号池供给与上游结构都是全局信号），因此不带作用域前缀。
  {
    key: "health",
    labelKey: "nav.health",
    path: "/health",
    icon: HeartPulse,
    section: "infra",
    keywords: ["health", "doctor", "jiankang", "jk"],
  },
  {
    key: "gateway",
    labelKey: "nav.gateway",
    path: "/gateway",
    icon: Network,
    section: "infra",
    keywords: ["gateway", "api", "wangguan", "wg"],
  },
];

/** 侧栏头像下拉里的那一项，命令面板一并收录。 */
export const SETTINGS_DESTINATION: NavDestination = {
  key: "settings",
  labelKey: "nav.settings",
  path: "/settings",
  icon: Settings,
  keywords: ["settings", "preferences", "config", "shezhi", "sz"],
};

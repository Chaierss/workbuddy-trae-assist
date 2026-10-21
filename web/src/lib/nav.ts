import type { Component } from "vue";
import {
  Activity,
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
}

export const NAV_DESTINATIONS: NavDestination[] = [
  { key: "overview", labelKey: "nav.overview", path: "/overview", icon: LayoutDashboard },
  { key: "accounts", labelKey: "nav.accounts", path: "/accounts", icon: Users },
  { key: "activity", labelKey: "nav.activity", path: "/activity", icon: Sparkles },
  { key: "usage", labelKey: "nav.usage", path: "/usage", icon: Activity },
  { key: "gateway", labelKey: "nav.gateway", path: "/gateway", icon: Network },
];

/** 侧栏头像下拉里的那一项，命令面板一并收录。 */
export const SETTINGS_DESTINATION: NavDestination = {
  key: "settings",
  labelKey: "nav.settings",
  path: "/settings",
  icon: Settings,
};

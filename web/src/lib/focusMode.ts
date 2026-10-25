import type { Component } from "vue";
import SettingsRail from "../views/settings/SettingsRail.vue";
import GatewayRail from "../views/gateway/GatewayRail.vue";
import { visibleSettingsTabs } from "../views/settings/tabs";
import { GATEWAY_TABS } from "../views/gateway/tabs";

/**
 * 专注模式（内部命名，不出现在任何用户可见文案里）。
 *
 * 形态就是设置页那样：模块自己的左栏替换全局侧栏，内容区独占剩余宽度。
 * 规则：模块子页 ≥ 2 才自动进入 —— 只有一个子页时左栏没有导航价值，
 * 还平白吃掉 240px，所以仍用全局侧栏。
 *
 * 判定只放在这里和 `AppShell`：路由只声明 `meta.focus` 是哪个模块，
 * 不要在各页面里自己判断形态，否则刷新页面就会丢布局。
 */
export const FOCUS_MIN_PAGES = 2;

export interface FocusModule {
  rail: Component;
  /** 子页数量，由模块自己的 tabs 清单派生，避免再维护第二份列表。 */
  pageCount: number;
}

export const FOCUS_MODULES: Record<string, FocusModule> = {
  settings: { rail: SettingsRail, pageCount: visibleSettingsTabs().length },
  gateway: { rail: GatewayRail, pageCount: GATEWAY_TABS.length },
};

/** 返回可用的专注模式模块；子页不足 2 个时返回 null，回退普通布局。 */
export function resolveFocusModule(name: unknown): FocusModule | null {
  const key = typeof name === "string" ? name : "";
  const module = key ? FOCUS_MODULES[key] : undefined;
  if (!module || module.pageCount < FOCUS_MIN_PAGES) return null;
  return module;
}

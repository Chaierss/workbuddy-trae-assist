import type { Component } from "vue";
import {
  Gauge,
  KeyRound,
  Layers,
  Network,
  ScrollText,
  Server,
} from "lucide-vue-next";
import type { MessageKey } from "../../lib/i18n";
import OverviewTab from "./OverviewTab.vue";
import ServiceTab from "./ServiceTab.vue";
import AccessTab from "./AccessTab.vue";
import KeysTab from "./KeysTab.vue";
import ModelsTab from "./ModelsTab.vue";
import LogsTab from "./LogsTab.vue";

/**
 * 网关 tab 的唯一登记处。加一个 tab 只改这里。
 *
 * 与设置中枢同构（左栏 + 右侧内容），但左栏由壳层渲染（`GatewayRail`），
 * 形态来自 `route.meta.immersive = 'shell'` —— 不存本地 state，刷新不丢。
 */
export interface GatewayTab {
  key: string;
  labelKey: MessageKey;
  hintKey: MessageKey;
  icon: Component;
  component: Component;
}

export const GATEWAY_ROUTE = "/gateway";
export const GATEWAY_DEFAULT_KEY = "overview";

export const GATEWAY_TABS: GatewayTab[] = [
  {
    key: "overview",
    labelKey: "gateway.tab.overview.label",
    hintKey: "gateway.tab.overview.hint",
    icon: Gauge,
    component: OverviewTab,
  },
  {
    key: "service",
    labelKey: "gateway.tab.service.label",
    hintKey: "gateway.tab.service.hint",
    icon: Server,
    component: ServiceTab,
  },
  {
    key: "access",
    labelKey: "gateway.tab.access.label",
    hintKey: "gateway.tab.access.hint",
    icon: Network,
    component: AccessTab,
  },
  {
    key: "keys",
    labelKey: "gateway.tab.keys.label",
    hintKey: "gateway.tab.keys.hint",
    icon: KeyRound,
    component: KeysTab,
  },
  {
    key: "models",
    labelKey: "gateway.tab.models.label",
    hintKey: "gateway.tab.models.hint",
    icon: Layers,
    component: ModelsTab,
  },
  {
    key: "logs",
    labelKey: "gateway.tab.logs.label",
    hintKey: "gateway.tab.logs.hint",
    icon: ScrollText,
    component: LogsTab,
  },
];

export function findGatewayTab(key: unknown): GatewayTab | null {
  if (typeof key !== "string") return null;
  return GATEWAY_TABS.find((tab) => tab.key === key) ?? null;
}

/** 激活项解析顺序：合法 ?tab= → 默认项 → 第一项。 */
export function resolveGatewayKey(raw: unknown): string {
  const matched = findGatewayTab(raw);
  if (matched) return matched.key;
  const fallback = findGatewayTab(GATEWAY_DEFAULT_KEY);
  return fallback?.key ?? GATEWAY_TABS[0].key;
}

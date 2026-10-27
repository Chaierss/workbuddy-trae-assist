import type { MessageKey } from "./i18n";
import rawRegistry from "../../../keel/components.json";

/**
 * 前端通用组件注册表 —— 设置 → 组件 页的唯一数据源。
 *
 * 真相源是仓库里的 `keel/components.json`（纯文本、可 diff），这里只做一次
 * 类型收敛。增删组件改 JSON，页面刷新即变，不需要同步第二份清单。
 *
 * 约定：`web/src/components/` 下的每个 `.vue` 都必须登记，否则
 * `node keel/console-check.mjs --strict` 会直接报错，页面上也会列出未登记名单。
 * 文案走 i18n，`summaryKey` 由 `locales/settings.ts` 提供中英两份。
 * `views/` 下的页面级组件不属于这里。
 */

export type ComponentCategory =
  | "layout"
  | "display"
  | "form"
  | "feedback"
  | "container"
  | "business";

export type ComponentStatus = "stable" | "beta" | "deprecated";

export interface ComponentDef {
  /** 唯一标识，重复登记直接报错，避免清单里出现两个同名项。 */
  id: string;
  /** 组件名，与文件名一致。 */
  name: string;
  /** 相对仓库根目录的路径。 */
  path: string;
  category: ComponentCategory;
  summaryKey: MessageKey;
  status?: ComponentStatus;
}

export const COMPONENT_CATEGORIES: ComponentCategory[] = [
  "layout",
  "display",
  "form",
  "feedback",
  "container",
  "business",
];

export function componentCategoryKey(category: ComponentCategory): MessageKey {
  return `settings.components.category.${category}`;
}

const ids = new Set<string>();
const registry: ComponentDef[] = rawRegistry.map((entry) => {
  if (ids.has(entry.key)) throw new Error(`组件已登记：${entry.key}`);
  ids.add(entry.key);
  return {
    id: entry.key,
    name: entry.name,
    path: entry.path,
    category: entry.category as ComponentCategory,
    summaryKey: entry.summaryKey as MessageKey,
    status: entry.status as ComponentStatus | undefined,
  };
});

export function listComponents(): ComponentDef[] {
  return registry;
}

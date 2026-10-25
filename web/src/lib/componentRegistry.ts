import type { MessageKey } from "./i18n";

/**
 * 前端通用组件注册表 —— 设置 → 组件 页的唯一数据源。
 *
 * 约定：在 `web/src/components/` 下新建通用组件后，必须在这里 `registerComponent`
 * 一次，否则设置页看不到它（新增组件 = 新建文件 + 登记）。这样做是为了让
 * 「现在有哪些通用组件、各自负责什么」有一份可读的清单，而不是靠翻目录猜。
 *
 * 只收录可复用的通用组件；`views/` 下的页面级组件不属于这里。
 * 文案走 i18n，`summaryKey` 由 `locales/settings.ts` 提供中英两份。
 */

export type ComponentCategory = "layout" | "display" | "form" | "feedback" | "container";

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
];

export function componentCategoryKey(category: ComponentCategory): MessageKey {
  return `settings.components.category.${category}`;
}

const registry = new Map<string, ComponentDef>();

export function registerComponent(def: ComponentDef): void {
  if (registry.has(def.id)) {
    throw new Error(`组件已登记：${def.id}`);
  }
  registry.set(def.id, def);
}

export function listComponents(): ComponentDef[] {
  return [...registry.values()];
}

/* ---------- 登记区：新增组件加在这里 ---------- */

registerComponent({
  id: "app-shell",
  name: "AppShell",
  path: "web/src/components/AppShell.vue",
  category: "layout",
  summaryKey: "settings.components.summary.appShell",
});

registerComponent({
  id: "sidebar-rail",
  name: "SidebarRail",
  path: "web/src/components/SidebarRail.vue",
  category: "layout",
  summaryKey: "settings.components.summary.sidebarRail",
});

registerComponent({
  id: "section-card",
  name: "SectionCard",
  path: "web/src/components/SectionCard.vue",
  category: "container",
  summaryKey: "settings.components.summary.sectionCard",
});

registerComponent({
  id: "account-card",
  name: "AccountCard",
  path: "web/src/components/AccountCard.vue",
  category: "display",
  summaryKey: "settings.components.summary.accountCard",
});

registerComponent({
  id: "stat-card",
  name: "StatCard",
  path: "web/src/components/StatCard.vue",
  category: "display",
  summaryKey: "settings.components.summary.statCard",
});

registerComponent({
  id: "animated-number",
  name: "AnimatedNumber",
  path: "web/src/components/AnimatedNumber.vue",
  category: "display",
  summaryKey: "settings.components.summary.animatedNumber",
});

registerComponent({
  id: "status-pill",
  name: "StatusPill",
  path: "web/src/components/StatusPill.vue",
  category: "display",
  summaryKey: "settings.components.summary.statusPill",
});

registerComponent({
  id: "empty-state",
  name: "EmptyState",
  path: "web/src/components/EmptyState.vue",
  category: "display",
  summaryKey: "settings.components.summary.emptyState",
});

registerComponent({
  id: "toggle-switch",
  name: "ToggleSwitch",
  path: "web/src/components/ToggleSwitch.vue",
  category: "form",
  summaryKey: "settings.components.summary.toggleSwitch",
});

registerComponent({
  id: "select-menu",
  name: "SelectMenu",
  path: "web/src/components/SelectMenu.vue",
  category: "form",
  summaryKey: "settings.components.summary.selectMenu",
});

registerComponent({
  id: "segmented-control",
  name: "SegmentedControl",
  path: "web/src/components/SegmentedControl.vue",
  category: "form",
  summaryKey: "settings.components.summary.segmentedControl",
});

registerComponent({
  id: "confirm-dialog",
  name: "ConfirmDialog",
  path: "web/src/components/ConfirmDialog.vue",
  category: "feedback",
  summaryKey: "settings.components.summary.confirmDialog",
});

registerComponent({
  id: "note-dialog",
  name: "NoteDialog",
  path: "web/src/components/NoteDialog.vue",
  category: "feedback",
  summaryKey: "settings.components.summary.noteDialog",
});

registerComponent({
  id: "switch-dialog",
  name: "SwitchDialog",
  path: "web/src/components/SwitchDialog.vue",
  category: "feedback",
  summaryKey: "settings.components.summary.switchDialog",
});

registerComponent({
  id: "oauth-login-dialog",
  name: "OAuthLoginDialog",
  path: "web/src/components/OAuthLoginDialog.vue",
  category: "feedback",
  summaryKey: "settings.components.summary.oauthLoginDialog",
});

registerComponent({
  id: "update-dialog",
  name: "UpdateDialog",
  path: "web/src/components/UpdateDialog.vue",
  category: "feedback",
  summaryKey: "settings.components.summary.updateDialog",
});

registerComponent({
  id: "command-palette",
  name: "CommandPalette",
  path: "web/src/components/CommandPalette.vue",
  category: "feedback",
  summaryKey: "settings.components.summary.commandPalette",
});

registerComponent({
  id: "open-api-viewer",
  name: "OpenApiViewer",
  path: "web/src/components/OpenApiViewer.vue",
  category: "display",
  summaryKey: "settings.components.summary.openApiViewer",
});

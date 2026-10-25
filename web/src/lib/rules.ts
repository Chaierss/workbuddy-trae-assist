import type { MessageKey } from "./i18n";

/**
 * 项目规则登记表 —— 设置 → 规则 页的唯一数据源。
 *
 * 规则只读展示，不在界面上编辑：约束要改就走代码评审，避免「界面上点一下」
 * 就把项目硬性约定改掉。新增一条规则时在这里追加一项，并在
 * `locales/settings.ts` 补上对应的 `title` / `detail` 文案（中英各一份）。
 */

export type RuleCategory =
  | "release"
  | "naming"
  | "scope"
  | "design"
  | "engineering"
  | "data";

/** `mandatory` 是不允许绕过的硬性约束；`convention` 是工程约定。 */
export type RuleLevel = "mandatory" | "convention";

export interface ProjectRule {
  id: string;
  category: RuleCategory;
  level: RuleLevel;
  titleKey: MessageKey;
  detailKey: MessageKey;
}

export const RULE_CATEGORIES: RuleCategory[] = [
  "release",
  "naming",
  "scope",
  "design",
  "engineering",
  "data",
];

export function ruleCategoryKey(category: RuleCategory): MessageKey {
  return `settings.rules.category.${category}`;
}

export const PROJECT_RULES: ProjectRule[] = [
  {
    id: "release-security-gate",
    category: "release",
    level: "mandatory",
    titleKey: "settings.rules.items.releaseSecurityGate.title",
    detailKey: "settings.rules.items.releaseSecurityGate.detail",
  },
  {
    id: "release-version-bump",
    category: "release",
    level: "mandatory",
    titleKey: "settings.rules.items.releaseVersionBump.title",
    detailKey: "settings.rules.items.releaseVersionBump.detail",
  },
  {
    id: "naming-project",
    category: "naming",
    level: "mandatory",
    titleKey: "settings.rules.items.namingProject.title",
    detailKey: "settings.rules.items.namingProject.detail",
  },
  {
    id: "naming-focus-mode",
    category: "naming",
    level: "mandatory",
    titleKey: "settings.rules.items.namingFocusMode.title",
    detailKey: "settings.rules.items.namingFocusMode.detail",
  },
  {
    id: "scope-pool",
    category: "scope",
    level: "mandatory",
    titleKey: "settings.rules.items.scopePool.title",
    detailKey: "settings.rules.items.scopePool.detail",
  },
  {
    id: "scope-batch-actions",
    category: "scope",
    level: "mandatory",
    titleKey: "settings.rules.items.scopeBatchActions.title",
    detailKey: "settings.rules.items.scopeBatchActions.detail",
  },
  {
    id: "scope-checkin-credits",
    category: "scope",
    level: "mandatory",
    titleKey: "settings.rules.items.scopeCheckinCredits.title",
    detailKey: "settings.rules.items.scopeCheckinCredits.detail",
  },
  {
    id: "scope-single-toggle",
    category: "scope",
    level: "mandatory",
    titleKey: "settings.rules.items.scopeSingleToggle.title",
    detailKey: "settings.rules.items.scopeSingleToggle.detail",
  },
  {
    id: "elegant-design",
    category: "design",
    level: "mandatory",
    titleKey: "settings.rules.items.elegantDesign.title",
    detailKey: "settings.rules.items.elegantDesign.detail",
  },
  {
    id: "design-tokens-only",
    category: "design",
    level: "mandatory",
    titleKey: "settings.rules.items.designTokensOnly.title",
    detailKey: "settings.rules.items.designTokensOnly.detail",
  },
  {
    id: "design-hierarchy",
    category: "design",
    level: "mandatory",
    titleKey: "settings.rules.items.designHierarchy.title",
    detailKey: "settings.rules.items.designHierarchy.detail",
  },
  {
    id: "design-one-action",
    category: "design",
    level: "mandatory",
    titleKey: "settings.rules.items.designOneAction.title",
    detailKey: "settings.rules.items.designOneAction.detail",
  },
  {
    id: "design-motion-budget",
    category: "design",
    level: "convention",
    titleKey: "settings.rules.items.designMotionBudget.title",
    detailKey: "settings.rules.items.designMotionBudget.detail",
  },
  {
    id: "design-theme-parity",
    category: "design",
    level: "mandatory",
    titleKey: "settings.rules.items.designThemeParity.title",
    detailKey: "settings.rules.items.designThemeParity.detail",
  },
  {
    id: "docs-auto-refresh",
    category: "engineering",
    level: "mandatory",
    titleKey: "settings.rules.items.docsAutoRefresh.title",
    detailKey: "settings.rules.items.docsAutoRefresh.detail",
  },
  {
    id: "engineering-component-registry",
    category: "engineering",
    level: "convention",
    titleKey: "settings.rules.items.engineeringComponentRegistry.title",
    detailKey: "settings.rules.items.engineeringComponentRegistry.detail",
  },
  {
    id: "data-backup-credentials",
    category: "data",
    level: "convention",
    titleKey: "settings.rules.items.dataBackupCredentials.title",
    detailKey: "settings.rules.items.dataBackupCredentials.detail",
  },
];

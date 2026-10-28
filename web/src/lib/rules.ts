import type { MessageKey } from "./i18n";
import { t } from "./i18n";
import rawRules from "../../../keel/rules.json";

/**
 * 项目规则登记表 —— 设置 → 规则 页的唯一数据源。
 *
 * 真相源是仓库里的 `keel/rules.json`：规则要变就改 JSON / 走代码评审，
 * 页面只读不写。`title` / `detail` 存的是 i18n key，按当前语言解析；
 * 同一份数据还能导出成 Markdown 交给 AI 当作上下文。
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
  /** 这条规则追到哪个文件，评审时用来核对。 */
  source: string;
}

export interface PageModeTerm {
  key: string;
  name: string;
  english: string;
  definition: string;
  signal: string;
  avoid: string[];
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

export function ruleLevelKey(level: RuleLevel): MessageKey {
  return level === "mandatory"
    ? "settings.rules.level.mandatory"
    : "settings.rules.level.convention";
}

export const PROJECT_RULES: ProjectRule[] = rawRules.rules.map((rule) => ({
  id: rule.id,
  category: rule.category as RuleCategory,
  level: rule.severity === "must" ? "mandatory" : "convention",
  titleKey: rule.title as MessageKey,
  detailKey: rule.detail as MessageKey,
  source: rule.source,
}));

/** 内部设计术语（页面形态）。导出给 AI 时一并保留。 */
export const PAGE_MODES: PageModeTerm[] = rawRules.pageModes as PageModeTerm[];

/**
 * 导出给 AI 的 Markdown：术语定义 + 硬性约束 + 出处。
 *
 * 验收标准不是「人觉得清楚」，而是没读过代码的 agent 只读这段，
 * 就不会把「专注模式」误解成全屏或弹窗。
 */
export function rulesMarkdown(): string {
  const lines: string[] = [
    `# ${rawRules.projectName} · 项目规则`,
    "",
    `版本：${rawRules.version}`,
    "",
    "> 术语定义与硬性约束。看到「专注模式」时按下面定义理解，不要自行想象成全屏或弹窗。",
    "",
    "## 术语与页面形态",
    "",
  ];

  for (const mode of PAGE_MODES) {
    lines.push(
      `### ${mode.name}（${mode.english}）`,
      "",
      `- 定义：${mode.definition}`,
      `- 界面信号：${mode.signal}`,
      `- 不要这样理解：${mode.avoid.join("；")}`,
      "",
    );
  }

  lines.push("## 规则", "");
  for (const rule of PROJECT_RULES) {
    lines.push(
      `### ${rule.id} · ${t(rule.titleKey)}`,
      "",
      `- 级别：${t(ruleLevelKey(rule.level))} · 类别：${t(ruleCategoryKey(rule.category))}`,
      `- 约束：${t(rule.detailKey)}`,
      `- 出处：${rule.source}`,
      "",
    );
  }

  return lines.join("\n");
}

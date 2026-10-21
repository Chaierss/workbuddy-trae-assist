import { ref, watchEffect } from "vue";
import { accountsEn, accountsZh } from "./locales/accounts";
import { activityEn, activityZh } from "./locales/activity";
import { chromeEn, chromeZh } from "./locales/chrome";
import { gatewayEn, gatewayZh } from "./locales/gateway";
import { settingsEn, settingsZh } from "./locales/settings";
import { usageEn, usageZh } from "./locales/usage";

/**
 * 自研极简 i18n：一个 `locale` ref + 一个 `t()`，不引第三方依赖。
 *
 * 字典按域拆成 `locales` 目录下的分片（账号 / 活动 / 用量 / 网关 / 设置 / 外壳），
 * 在这里合成两份完整字典。这样做不是为了「模块化」好看，而是因为
 * 完整字典上千行、多人（多轮）并行编辑同一个文件必然冲突。
 *
 * 约定：
 * - key 一律 `<域>.<名字>`，域与分片文件一一对应，避免跨片撞 key；
 * - 用户在界面上看得到的文案才进字典；`console` 日志、代码注释保持中文；
 * - **不要**给分片加 `Record<string, string>` 注解 —— 那样 key 会被宽化成 string，
 *   组件里写错 key 就没人拦了。让 TS 自己推断字面量 key 联合。
 * - 注册表（`lib/nav.ts`、各视图的 `tabs.ts`）里存的是 key，不是译文：
 *   模块级常量里的 `t()` 只在加载时求值一次，切语言不会更新。
 */

export type LocaleCode = "zh-CN" | "en";

export interface LocaleOption {
  code: LocaleCode;
  label: string;
}

export const LOCALES: LocaleOption[] = [
  { code: "zh-CN", label: "简体中文" },
  { code: "en", label: "English" },
];

const STORAGE_KEY = "chaierww.locale";

const zhCN = {
  ...chromeZh,
  ...accountsZh,
  ...activityZh,
  ...usageZh,
  ...gatewayZh,
  ...settingsZh,
};

/** 英文缺 key 时这里会报类型错 —— 字典完整性的兜底检查。 */
const en: Record<keyof typeof zhCN, string> = {
  ...chromeEn,
  ...accountsEn,
  ...activityEn,
  ...usageEn,
  ...gatewayEn,
  ...settingsEn,
};

/** 所有可用的 key。组件里写错 key 时 `vue-tsc` 会直接报错。 */
export type MessageKey = keyof typeof zhCN;

const DICTS: Record<LocaleCode, Record<MessageKey, string>> = { "zh-CN": zhCN, en };

function readStoredLocale(): LocaleCode {
  const raw = localStorage.getItem(STORAGE_KEY);
  return raw === "en" ? "en" : "zh-CN";
}

export const locale = ref<LocaleCode>(readStoredLocale());

export function setLocale(next: LocaleCode): void {
  locale.value = next;
  localStorage.setItem(STORAGE_KEY, next);
}

/**
 * 给 `toLocaleString` / `localeCompare` 用的 BCP-47 标签。
 * 日期与名称排序要跟着界面语言走，不能把 `zh-CN` 写死在业务代码里。
 */
export function localeTag(): "zh-CN" | "en-US" {
  return locale.value === "en" ? "en-US" : "zh-CN";
}

/**
 * 取词。`params` 用于替换 `{name}` 占位符。
 * 缺词时回落中文、再回落 key 本身，保证界面不会出现空白。
 */
export function t(key: MessageKey, params?: Record<string, string | number>): string {
  const dict = DICTS[locale.value] as Partial<Record<MessageKey, string>>;
  const fallback = zhCN[key] as string | undefined;
  let text = dict[key] ?? fallback ?? key;
  if (params) {
    text = text.replace(/\{(\w+)\}/g, (whole: string, name: string) =>
      name in params ? String(params[name]) : whole,
    );
  }
  return text;
}

/** 语言同步到 `<html lang>`，读屏软件与浏览器排版跟着走。 */
watchEffect(() => {
  document.documentElement.lang = locale.value;
});

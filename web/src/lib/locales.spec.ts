import { describe, expect, it } from "vitest";
import { chromeEn, chromeZh } from "./locales/chrome";
import { accountsEn, accountsZh } from "./locales/accounts";
import { activityEn, activityZh } from "./locales/activity";
import { usageEn, usageZh } from "./locales/usage";
import { settingsEn, settingsZh } from "./locales/settings";
import { gatewayEn, gatewayZh } from "./locales/gateway";
import { updateEn, updateZh } from "./locales/update";

/**
 * 多语种守卫（0.8.0）：
 * 1. 中英 key 必须一一对应 —— 漏翻会回退到 key 本身，用户看到 `env.metrics.opens`；
 * 2. 英文文案里不允许出现中文 —— 整句漏翻时最容易「中文混在英文界面里」。
 *
 * 专有名词（WorkBuddy / Trae）不是中文，不需要白名单；真有必须保留的中文
 * 才往 `ALLOWED_CJK` 里加，并且只允许整值匹配，避免夹带。
 */

const ALLOWED_CJK = new Set<string>([
  // 目前没有需要保留的中文；加之前先想清楚为什么不能翻译。
]);

const pairs = [
  { name: "chrome", zh: chromeZh, en: chromeEn },
  { name: "accounts", zh: accountsZh, en: accountsEn },
  { name: "activity", zh: activityZh, en: activityEn },
  { name: "usage", zh: usageZh, en: usageEn },
  { name: "settings", zh: settingsZh, en: settingsEn },
  { name: "gateway", zh: gatewayZh, en: gatewayEn },
  { name: "update", zh: updateZh, en: updateEn },
] as const;

const CJK = /[\u3400-\u9fff]/;

describe("locale keys", () => {
  for (const pair of pairs) {
    it(`${pair.name}: zh and en expose the same keys`, () => {
      const zhKeys = Object.keys(pair.zh).sort();
      const enKeys = Object.keys(pair.en).sort();
      expect(enKeys).toEqual(zhKeys);
    });

    it(`${pair.name}: english values contain no chinese`, () => {
      const offenders = Object.entries(pair.en)
        .filter(([, value]) => CJK.test(String(value)) && !ALLOWED_CJK.has(String(value)))
        .map(([key]) => key);
      expect(offenders).toEqual([]);
    });

    it(`${pair.name}: no empty values`, () => {
      for (const [key, value] of [
        ...Object.entries(pair.zh),
        ...Object.entries(pair.en),
      ]) {
        expect(String(value).trim(), key).not.toBe("");
      }
    });
  }
});

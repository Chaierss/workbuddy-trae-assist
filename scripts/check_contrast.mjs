#!/usr/bin/env node
/**
 * 对比度门禁：把浅色 / 深色语义色按 WCAG AA 4.5:1 校验一遍。
 *
 * 只校验「文字色 × 它实际出现的背景」这一组白名单，不用扫描整份 CSS：
 * 语义变量之外的装饰色（阴影、渐变、透明度）不在承诺范围内。
 * 失败时打印变量对与真实比值，退出码 1，直接能定位到要改哪一行。
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const CSS = resolve(ROOT, "web/src/style.css");

/** 文本色 → 背景色。每一对都必须 ≥ 4.5。 */
const PAIRS = [
  ["ink", "surface"],
  ["ink-soft", "surface"],
  ["muted", "surface"],
  ["brand-dark", "brand-soft"],
  ["warn", "warn-soft"],
  ["danger", "danger-soft"],
  ["info", "info-soft"],
];

function block(source, selector) {
  const start = source.indexOf(`${selector} {`);
  if (start < 0) throw new Error(`找不到样式块：${selector}`);
  const end = source.indexOf("}", start);
  return source.slice(start, end);
}

function tokens(source, selector) {
  const out = {};
  for (const match of block(source, selector).matchAll(/--color-([\w-]+):\s*(#[0-9a-fA-F]{6})/g)) {
    out[match[1]] = match[2].toLowerCase();
  }
  return out;
}

function luminance(hex) {
  const channels = [1, 3, 5].map((index) => parseInt(hex.slice(index, index + 2), 16) / 255);
  const linear = channels.map((value) =>
    value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
}

function ratio(foreground, background) {
  const a = luminance(foreground);
  const b = luminance(background);
  const [light, dark] = a > b ? [a, b] : [b, a];
  return (light + 0.05) / (dark + 0.05);
}

const source = readFileSync(CSS, "utf8");
const themes = [
  ["浅色", tokens(source, "@theme")],
  ["深色", tokens(source, 'html[data-theme="dark"]')],
];

const failures = [];
for (const [label, palette] of themes) {
  for (const [fg, bg] of PAIRS) {
    const foreground = palette[fg];
    const background = palette[bg];
    if (!foreground || !background) {
      failures.push(`${label}：缺少 --color-${foreground ? bg : fg}`);
      continue;
    }
    const value = ratio(foreground, background);
    if (value < 4.5) {
      failures.push(
        `${label}：--color-${fg} (${foreground}) on --color-${bg} (${background}) = ${value.toFixed(2)}:1 < 4.5:1`,
      );
    }
  }
}

if (failures.length) {
  console.error("对比度门禁未通过：");
  for (const line of failures) console.error(`  - ${line}`);
  process.exit(1);
}
console.log(`对比度门禁通过：${themes.length} 套主题 × ${PAIRS.length} 组文字/背景`);

#!/usr/bin/env node
/**
 * 前端包体预算门禁（1.3.0）。
 *
 * 基线：1.2.0 是单个 index-*.js 909.7 KB、index-*.css 62.8 KB，没有路由拆分。
 * 1.3.0 路由懒加载后入口实测约 496.4 kB（Vite 口径；本脚本按 KiB 打印）。
 * 入口仍包含中英两套完整字典，壳层同步渲染要用，
 * 因此入口上限设 560 KB，给后续小改动留约 10% 余量；任意分块 ≤ 560 KB、
 * JS 总量 ≤ 1.1 MB、CSS ≤ 120 KB。超预算打印实测/上限对照表并以退出码 1 失败。
 */
import { readdirSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const assetsDir = join(repoRoot, "web", "dist", "assets");

const KB = 1024;
const ENTRY_MAX = 560 * KB;
const CHUNK_MAX = 560 * KB;
const JS_TOTAL_MAX = 1.1 * 1024 * KB;
const CSS_MAX = 120 * KB;

function kb(bytes) {
  return (bytes / KB).toFixed(1);
}

let files;
try {
  files = readdirSync(assetsDir, { withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => {
      const path = join(assetsDir, entry.name);
      return { name: entry.name, bytes: statSync(path).size };
    });
} catch {
  console.error(`找不到构建产物：${assetsDir}`);
  console.error("先运行 npm run build --prefix web");
  process.exit(1);
}

const js = files.filter((file) => file.name.endsWith(".js"));
const css = files.filter((file) => file.name.endsWith(".css"));

if (!js.length) {
  console.error(`构建产物里没有 JS：${assetsDir}`);
  process.exit(1);
}

const entry = js.find((file) => /^index(?:-[A-Za-z0-9_-]+)?\.js$/.test(file.name)) ??
  js.reduce((largest, file) => (file.bytes > largest.bytes ? file : largest), js[0]);
const jsTotal = js.reduce((sum, file) => sum + file.bytes, 0);
const cssTotal = css.reduce((sum, file) => sum + file.bytes, 0);
const largestChunk = js.reduce((largest, file) => (file.bytes > largest.bytes ? file : largest), js[0]);

const rows = [
  { item: "入口", file: entry.name, bytes: entry.bytes, max: ENTRY_MAX },
  { item: "最大分块", file: largestChunk.name, bytes: largestChunk.bytes, max: CHUNK_MAX },
  { item: "JS 总量", file: `${js.length} 个文件`, bytes: jsTotal, max: JS_TOTAL_MAX },
  { item: "CSS 总量", file: `${css.length} 个文件`, bytes: cssTotal, max: CSS_MAX },
];

console.log(`前端包体预算 · ${assetsDir}`);
for (const row of rows) {
  const over = row.bytes > row.max;
  console.log(
    `${over ? "✗" : "✓"} ${row.item.padEnd(8)} ${kb(row.bytes).padStart(7)} KB / ${kb(row.max).padStart(7)} KB  ${row.file}`,
  );
}

const failed = rows.filter((row) => row.bytes > row.max);
if (failed.length) {
  console.error(`\n超出预算 ${failed.length} 项，请检查新增依赖或是否误把整库打进入口块。`);
  process.exit(1);
}

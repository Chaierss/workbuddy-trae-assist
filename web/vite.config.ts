import { readdirSync, readFileSync } from "node:fs";
import { dirname, extname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";
import vue from "@vitejs/plugin-vue";
import tailwindcss from "@tailwindcss/vite";

/**
 * `virtual:keel-report` —— 组件注册表的体检结果。
 *
 * 数据来自 `keel/components.json` 与 `web/src/components/` 的真实文件：
 * - 未登记：目录里有、注册表没有；
 * - 未引用：注册表有、但没有任何源码 import 它。
 *
 * 扫描放在构建期（Node fs），不靠页面手写清单，所以新增组件不需要改这里。
 */
const SOURCE_EXTS = [".vue", ".ts", ".js"];
const KEEP_SUFFIX = /\.\w+$/;
const escapeRe = (value: string): string => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function walk(dir: string, acc: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === "node_modules") continue;
    const abs = join(dir, entry.name);
    if (entry.isDirectory()) walk(abs, acc);
    else if (SOURCE_EXTS.includes(extname(entry.name))) acc.push(abs);
  }
  return acc;
}

const posix = (value: string): string => value.split("\\").join("/");

/** `web/` 的绝对路径（配置文件名所在目录），真相源在仓库根的 `keel/` 与 `docs/`。 */
const webRoot = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(webRoot, "..");

function keelReport(): Plugin {
  const registryPath = join(repoRoot, "keel", "components.json");

  return {
    name: "keel-report",
    resolveId(id) {
      return id === "virtual:keel-report" ? "\0keel-report" : null;
    },
    load(id) {
      if (id !== "\0keel-report") return null;

      const registry = JSON.parse(readFileSync(registryPath, "utf8")) as Array<{
        key: string;
        path: string;
        status?: string;
      }>;
      const registered = new Set(registry.map((entry) => entry.path));

      const componentFiles = walk(join(webRoot, "src", "components"));
      const actual = componentFiles
        .map((abs) => posix(relative(repoRoot, abs)))
        .filter((rel) => !rel.includes(".demo."));
      const unregistered = actual.filter((rel) => !registered.has(rel));

      const sources = walk(join(webRoot, "src")).map((abs) => ({
        rel: posix(relative(webRoot, abs)),
        text: readFileSync(abs, "utf8"),
      }));

      const unused = registry
        .filter((entry) => entry.status !== "deprecated")
        .filter((entry) => {
          const self = entry.path.replace(/^web\//, "");
          const base = entry.path.split("/").pop()?.replace(KEEP_SUFFIX, "") ?? "";
          if (!base) return false;
          const pattern = new RegExp(`from\\s+['"][^'"]*${escapeRe(base)}(?:\\.\\w+)?['"]`);
          return !sources.some((source) => source.rel !== self && pattern.test(source.text));
        })
        .map((entry) => entry.path);

      for (const abs of [...componentFiles, ...sources.map((s) => join(webRoot, s.rel))]) {
        this.addWatchFile(abs);
      }
      this.addWatchFile(registryPath);

      return `export default ${JSON.stringify({ unregistered, unused })};`;
    },
  };
}

// 端口刻意避开 Windows 保留段（1346-1445 / 1046-1145 等）和旧项目占用的 57890-57895。
export default defineConfig({
  plugins: [vue(), tailwindcss(), keelReport()],
  server: {
    // 显式绑 IPv4：默认 localhost 在部分 Windows 环境只监听 ::1，
    // Playwright 用 127.0.0.1 探活会直接连接失败。
    host: "127.0.0.1",
    port: 57911,
    strictPort: true,
    // 规则 / 组件 / Wiki 的真相源在仓库根 keel/ 与 docs/，都在 Vite root 之外。
    fs: { allow: [webRoot, repoRoot] },
    proxy: {
      "/api": {
        target: process.env.CHAIERWW_API ?? "http://127.0.0.1:57910",
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});

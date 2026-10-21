import MarkdownIt from "markdown-it";

/** html:false —— 文档里即使写了原始 HTML 也不会被注入，避免 md 变成 XSS 入口。 */
export const md = new MarkdownIt({ html: false, linkify: true, breaks: false, typographer: false });

/** 把 `a/./b/../c.md` 收敛成 `a/c.md`，用于解析 md 之间的相对链接。 */
export function normalizeDocPath(input: string): string {
  const out: string[] = [];
  for (const segment of input.split("/")) {
    if (!segment || segment === ".") continue;
    if (segment === "..") {
      out.pop();
      continue;
    }
    out.push(segment);
  }
  return out.join("/");
}

/**
 * 只接管「指向另一篇 md」的链接。
 * 返回 null 表示不是站内文档，交给浏览器默认行为（外链另行处理）。
 */
export function resolveDocHref(fromPath: string, href: string): string | null {
  const clean = href.split("#")[0].split("?")[0].trim();
  if (!clean) return null;
  if (/^[a-z][a-z0-9+.-]*:/i.test(clean)) return null;
  if (!clean.toLowerCase().endsWith(".md")) return null;
  if (clean.startsWith("/")) return normalizeDocPath(clean);
  const dir = fromPath.includes("/") ? fromPath.slice(0, fromPath.lastIndexOf("/")) : "";
  return normalizeDocPath(dir ? `${dir}/${clean}` : clean);
}

export function isExternalHref(href: string): boolean {
  return /^https?:\/\//i.test(href);
}

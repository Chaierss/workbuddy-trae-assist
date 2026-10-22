import { md } from "./markdown";

/**
 * 把主进程推过来的 `releaseNotes` 变成一个可以安全 `v-html` 的片段。
 *
 * 为什么需要这一层：主进程只做「归一成字符串」，不猜格式，而实际的来源有两种——
 *   - GitHub provider：从 `releases.atom` 的 `<content>` 取，是**渲染后的 HTML**
 *     （`<h2>`、`<ul>`、`<li>` 都带标签）；
 *   - 通用 provider：读 `latest.yml` 里的 `releaseNotes`，多半是 markdown 原文。
 * 以前前端直接 `{{ state.releaseNotes }}` 插值，第一种来源就会把标签当纯文本吐出来，
 * 用户看到的就是一整片「代码」。
 *
 * 这里的策略是「按内容猜格式，猜完必洗」：
 *   像 HTML → 白名单清洗后原样渲染；不像 → 交给 markdown-it（`html: false`，
 *   原文里的标签会被转义成文本而不是执行）。两条路都过一遍清洗，`v-html` 才敢用。
 */

/**
 * 命中这些**块级**标签才认为是 HTML。
 *
 * 只认块级是刻意的：`latest.yml` 里手写的说明偶尔夹一个 `<br>`，那种情况仍该按
 * markdown 排版；而真正的 HTML 说明一定会出现 `<p>` / `<h2>` / `<ul>` 这类结构标签。
 */
const HTML_BLOCK_HINT =
  /<(?:p|div|h[1-6]|ul|ol|li|dl|dt|dd|pre|blockquote|table|thead|tbody|tr|td|th)\b[^>]*\/?>/i;

/** 允许保留的标签。没有 img / script / style / iframe：说明里出现它们只会是跟踪像素或注入。 */
const ALLOWED_TAGS = new Set([
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "p",
  "div",
  "span",
  "br",
  "hr",
  "ul",
  "ol",
  "li",
  "dl",
  "dt",
  "dd",
  "strong",
  "b",
  "em",
  "i",
  "u",
  "s",
  "del",
  "code",
  "pre",
  "blockquote",
  "a",
  "table",
  "thead",
  "tbody",
  "tr",
  "td",
  "th",
]);

/** 每个标签允许保留的属性——只有链接需要，其余一律剥掉（含 style / on*）。 */
const ALLOWED_ATTRS: Record<string, readonly string[]> = { a: ["href", "title"] };

/** 这些标签连同内容一起丢：留着内容只会变成一段看不懂的文字。 */
const DROP_TAGS = new Set([
  "script",
  "style",
  "iframe",
  "frame",
  "frameset",
  "object",
  "embed",
  "applet",
  "svg",
  "math",
  "template",
  "link",
  "meta",
  "base",
  "noscript",
  "form",
  "input",
  "button",
  "select",
  "option",
  "textarea",
]);

/** 这份说明看起来是 HTML 吗。 */
export function looksLikeHtml(raw: string): boolean {
  return HTML_BLOCK_HINT.test(raw);
}

/** 更新说明 → 可安全插入的 HTML。空输入返回空串，调用方据此决定要不要显示占位文案。 */
export function renderReleaseNotes(raw?: string | null): string {
  const text = (raw ?? "").trim();
  if (!text) return "";
  return sanitize(looksLikeHtml(text) ? text : md.render(text));
}

/**
 * 白名单清洗。
 *
 * 用 DOMParser 而不是正则：正则处理 HTML 迟早漏（注释里的 `-->`、属性里的 `>`、
 * 大小写混写），而这里的东西来自 GitHub 上可以被任何人改的 release 正文。
 * 解析出的树只在内存里走一遍，不插入文档，所以不会触发资源加载。
 */
function sanitize(html: string): string {
  const doc = new DOMParser().parseFromString(`<body>${html}</body>`, "text/html");
  scrub(doc.body);
  return doc.body.innerHTML;
}

function scrub(parent: Element): void {
  for (const node of Array.from(parent.childNodes)) {
    if (node.nodeType === Node.COMMENT_NODE) {
      node.remove();
      continue;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) continue;

    const el = node as Element;
    const tag = el.tagName.toLowerCase();

    if (DROP_TAGS.has(tag)) {
      el.remove();
      continue;
    }
    if (!ALLOWED_TAGS.has(tag)) {
      // 白名单外的容器（<section>、<font>…）：留内容、去这层壳，别把整段文字一起丢掉。
      scrub(el);
      el.replaceWith(...Array.from(el.childNodes));
      continue;
    }

    const keep = ALLOWED_ATTRS[tag] ?? [];
    for (const attr of Array.from(el.attributes)) {
      if (!keep.includes(attr.name.toLowerCase())) el.removeAttributeNode(attr);
    }
    if (tag === "a") {
      const href = (el.getAttribute("href") ?? "").trim();
      if (!/^https?:\/\//i.test(href)) {
        // javascript: / data: / 相对路径一律不给点——说明里的链接只该指向外部网页。
        el.removeAttribute("href");
      } else {
        // 渲染层在 Electron 里，新窗口会被主进程的 setWindowOpenHandler 交给系统浏览器。
        el.setAttribute("target", "_blank");
        el.setAttribute("rel", "noopener noreferrer");
      }
    }

    scrub(el);
  }
}

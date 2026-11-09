/**
 * HTML 白名单清洗：所有要交给 `v-html` 的字符串都必须先过这里。
 *
 * 用 DOMParser 而不是正则：正则处理 HTML 迟早漏（注释里的 `-->`、属性里的 `>`、
 * 大小写混写）。解析出的树只在内存里走一遍，不插入文档，所以不会触发资源加载。
 *
 * 使用方：
 *   - `releaseNotes.ts` 的更新说明（来源可能是 GitHub 渲染后的 HTML）；
 *   - Wiki 正文（markdown-it 渲染后，再剥一层白名单）。
 */

/** 允许保留的标签。没有 img / script / style / iframe：出现它们只会是跟踪像素或注入。 */
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

/** 清洗入口。空输入返回空串，调用方据此决定要不要显示占位文案。 */
export function sanitizeHtml(html: string): string {
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

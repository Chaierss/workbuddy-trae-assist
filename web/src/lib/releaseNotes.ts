import MarkdownIt from "markdown-it";

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
 * 这里的策略是「统一走 markdown-it（放开原始 HTML）→ 必过白名单清洗」：
 *   - GitHub 来源是渲染后的 HTML，markdown-it 会原样放行；
 *   - yml 来源是 markdown，正常排版，夹带的 `<br>` 也不会被当成文本显示。
 * 不再按内容猜格式：猜错的代价就是用户看到一整片 `&lt;h2&gt;` 标签。
 */

/**
 * 说明专用 markdown 实例：`html: true`。
 *
 * 通用文档渲染里 `html: false` 是为了不执行原始标签；但更新说明的两种来源都会
 * 带标签（GitHub 是渲染后的 HTML，yml 可能是夹着 `<br>` 的 markdown）。用 `html:
 * false` 时这些标签会被转义成 `&lt;h2&gt;` 原样显示，也就是用户看到的「一堆代码」。
 * 这里放开原始 HTML，再由 `sanitize()` 做白名单清洗——输出同样是安全的。
 */
const notesMd = new MarkdownIt({ html: true, linkify: true, breaks: false, typographer: false });

/**
 * 有些来源会给出**被转义过一次**的 HTML（`&lt;h2&gt;…&lt;/h2&gt;`），解析器不做
 * 反解时 markdown 会把它当纯文本，最后又显示成标签。只在这种形态下解一层实体，
 * 常规 markdown（比如代码块里的 `&lt;` 示例）保持原样。
 */
const ESCAPED_HTML_HINT = /&lt;\/?(?:p|div|h[1-6]|ul|ol|li|pre|blockquote|table)\b/i;

function decodeEscapedHtml(text: string): string {
  if (!ESCAPED_HTML_HINT.test(text)) return text;
  const doc = new DOMParser().parseFromString(`<body>${text}</body>`, "text/html");
  return doc.body.textContent ?? text;
}

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

/** 更新说明 → 可安全插入的 HTML。空输入返回空串，调用方据此决定要不要显示占位文案。 */
export function renderReleaseNotes(raw?: string | null): string {
  const text = decodeEscapedHtml((raw ?? "").trim());
  if (!text) return "";
  return sanitize(notesMd.render(text));
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

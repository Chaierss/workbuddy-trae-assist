// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { sanitizeHtml } from "./sanitizeHtml";

/**
 * 渲染安全守卫（1.3.0）：
 * Wiki 与更新说明都走同一个白名单，任何来源的 HTML 都不允许带出脚本、事件属性
 * 或可执行的链接。这里锁死这些边界，防止哪天换渲染库时把缺口带回来。
 */
describe("sanitizeHtml", () => {
  it("丢掉 script，保留正常段落", () => {
    const html = sanitizeHtml('<script>alert("x")</script><p>正文</p>');
    expect(html).not.toContain("script");
    expect(html).not.toContain("alert");
    expect(html).toContain("<p>正文</p>");
  });

  it("去掉事件属性和不在白名单的标签", () => {
    const html = sanitizeHtml('<p onclick="alert(1)" style="color:red">文字</p><img src=x onerror="alert(1)">');
    expect(html).not.toContain("onclick");
    expect(html).not.toContain("style");
    expect(html).not.toContain("img");
    expect(html).toContain("文字");
  });

  it("只允许 http(s) 链接，并补 target/rel", () => {
    const blocked = sanitizeHtml('<a href="javascript:alert(1)">点我</a>');
    expect(blocked).not.toContain("javascript:");
    expect(blocked).toContain("点我");

    const allowed = sanitizeHtml('<a href="https://example.com" title="说明">外链</a>');
    expect(allowed).toContain('href="https://example.com"');
    expect(allowed).toContain('target="_blank"');
    expect(allowed).toContain('rel="noopener noreferrer"');
  });

  it("连同内容丢掉 iframe/style/注释", () => {
    const html = sanitizeHtml('<iframe src="https://evil"></iframe><style>p{color:red}</style><!-- 隐藏 --><p>ok</p>');
    expect(html).not.toContain("iframe");
    expect(html).not.toContain("style");
    expect(html).not.toContain("隐藏");
    expect(html).toContain("<p>ok</p>");
  });

  it("白名单外的容器去壳留内容", () => {
    const html = sanitizeHtml("<section><strong>保留</strong>文字</section>");
    expect(html).not.toContain("section");
    expect(html).toContain("<strong>保留</strong>");
    expect(html).toContain("文字");
  });

  it("空输入返回空串", () => {
    expect(sanitizeHtml("")).toBe("");
  });
});

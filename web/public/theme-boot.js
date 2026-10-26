/* 主题在首屏渲染前就要落到 <html> 上，否则深色模式会白闪一帧。
   键名与 stores/app.ts 的 THEME_STORAGE_KEY 必须一致。
   外置成文件是为了让 SPA 入口能启用严格 CSP（不允许内联脚本）。 */
(function () {
  try {
    var mode = localStorage.getItem("chaierww.theme");
    var dark =
      mode === "dark" ||
      ((mode === "system" || !mode) &&
        window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
  } catch (error) {
    document.documentElement.dataset.theme = "light";
  }
})();

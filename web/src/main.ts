import { createApp } from "vue";
import App from "./App.vue";
import { router } from "./router";
import { api } from "./api";
import { loadScopes, loadHealth, refreshAll } from "./stores/app";
import "./style.css";

const app = createApp(App);
app.use(router);

/** 启动早期（i18n 尚未挂载）的错误页：用纯文本，不依赖任何组件或语言包。 */
function renderFatal(title: string, detail: string): void {
  const root = document.querySelector("#app");
  if (!root) return;
  root.innerHTML = "";
  const box = document.createElement("div");
  box.style.cssText =
    "max-width:560px;margin:16vh auto;padding:24px;font:14px/1.7 system-ui," +
    "sans-serif;color:#334155;background:#fff;border:1px solid #e2e8f0;" +
    "border-radius:12px;white-space:pre-wrap";
  box.textContent = `${title}\n\n${detail}`;
  root.appendChild(box);
}

/** 错误上报限速：同一会话 30 秒内最多 5 次，避免死循环把后端刷爆。 */
let reported = 0;
let reportedSince = 0;
function report(message: string, stack: string, source: string): void {
  const now = Date.now();
  if (now - reportedSince > 30_000) {
    reportedSince = now;
    reported = 0;
  }
  if (reported >= 5) return;
  reported += 1;
  void api
    .reportClientError({
      message,
      stack,
      source,
      url: location.pathname,
    })
    .catch(() => undefined);
}

app.config.errorHandler = (error, _instance, info) => {
  const message = error instanceof Error ? error.message : String(error);
  const stack = error instanceof Error ? (error.stack ?? "") : "";
  console.error("Vue 未捕获错误", error, info);
  report(message, stack, `vue:${info}`);
};

window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason;
  const message = reason instanceof Error ? reason.message : String(reason);
  const stack = reason instanceof Error ? (reason.stack ?? "") : "";
  report(message, stack, "promise");
});

async function bootstrap(): Promise<void> {
  try {
    // 先领取本机会话 Cookie（守卫关闭时后端直接返回 guard=false）。
    // 失败通常意味着后端未启动或来源被拒，此时挂载界面只会得到满屏红色错误。
    await api.authSession();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    renderFatal(
      "无法连接到本机服务",
      `请确认 chaierww 后端正在运行，然后刷新页面或重启应用。\n\n${message}`,
    );
    return;
  }

  try {
    await Promise.all([loadScopes(), loadHealth()]);
    await refreshAll();
  } catch (error) {
    console.error("初始化失败", error);
  }
  app.mount("#app");
}

void bootstrap();

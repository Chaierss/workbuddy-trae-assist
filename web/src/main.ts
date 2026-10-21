import { createApp } from "vue";
import App from "./App.vue";
import { router } from "./router";
import { loadScopes, loadHealth, refreshAll } from "./stores/app";
import "./style.css";

const app = createApp(App);
app.use(router);

async function bootstrap(): Promise<void> {
  try {
    await Promise.all([loadScopes(), loadHealth()]);
    await refreshAll();
  } catch (error) {
    console.error("初始化失败", error);
  }
  app.mount("#app");
}

void bootstrap();

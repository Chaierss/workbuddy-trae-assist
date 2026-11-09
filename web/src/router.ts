import { createRouter, createWebHistory } from "vue-router";

/**
 * 顶层路由按需加载（1.3.0）：首屏只下载壳层与当前页，其余视图进各自分块。
 * 子页（设置 / 网关各 tab）仍由父视图静态 import，避免一次性改动过多。
 */
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/overview" },
    {
      path: "/overview",
      name: "overview",
      component: () => import("./views/OverviewView.vue"),
    },
    /**
     * `alias` 给出「作用域写进 URL」的规范地址 `/s/<scope>/…`：
     * 同一条路由记录、同一个 name，进别名时 `route.params.scope` 有值。
     * 不带前缀的短地址继续可用（回退路径），所以侧栏链接与旧书签都不会失效。
     */
    {
      path: "/accounts",
      name: "accounts",
      component: () => import("./views/AccountsView.vue"),
      alias: "/s/:scope/accounts",
    },
    {
      path: "/activity",
      name: "activity",
      component: () => import("./views/ActivityView.vue"),
      alias: "/s/:scope/activity",
    },
    {
      path: "/usage",
      name: "usage",
      component: () => import("./views/UsageView.vue"),
      alias: "/s/:scope/usage",
    },
    {
      path: "/gateway",
      name: "gateway",
      component: () => import("./views/gateway/GatewayView.vue"),
      alias: "/s/:scope/gateway",
      /**
       * 网关与设置同构：子页 ≥ 2，进入专注模式（模块左栏替换全局侧栏）。
       * 这里只声明是哪个模块，形态判定在 `AppShell` + `lib/focusMode.ts`。
       */
      meta: { focus: "gateway" },
    },
    /**
     * 设置中枢：子页 ≥ 2，自动进入专注模式 —— 模块左栏替换全局侧栏。
     * 形态必须存在 meta 里：存 store 或本地 state 会导致刷新后布局丢失。
     * 不设 permission（本项目暂无权限体系），将来权限下沉到每个 tab。
     *
     * 注意：`/docs` 是后端的 Swagger 页，前端接口文档 tab 已改为直接渲染
     * `/api/openapi.json`，不再依赖它。
     */
    {
      path: "/settings",
      name: "settings",
      component: () => import("./views/SettingsView.vue"),
      meta: { focus: "settings" },
    },
    { path: "/:pathMatch(.*)*", redirect: "/overview" },
  ],
});

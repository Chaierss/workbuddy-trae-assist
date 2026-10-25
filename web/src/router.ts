import { createRouter, createWebHistory } from "vue-router";
import OverviewView from "./views/OverviewView.vue";
import AccountsView from "./views/AccountsView.vue";
import ActivityView from "./views/ActivityView.vue";
import UsageView from "./views/UsageView.vue";
import SettingsView from "./views/SettingsView.vue";
import GatewayView from "./views/gateway/GatewayView.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/overview" },
    { path: "/overview", name: "overview", component: OverviewView },
    /**
     * `alias` 给出「作用域写进 URL」的规范地址 `/s/<scope>/…`：
     * 同一条路由记录、同一个 name，进别名时 `route.params.scope` 有值。
     * 不带前缀的短地址继续可用（回退路径），所以侧栏链接与旧书签都不会失效。
     */
    { path: "/accounts", name: "accounts", component: AccountsView, alias: "/s/:scope/accounts" },
    {
      path: "/activity",
      name: "activity",
      component: ActivityView,
      alias: "/s/:scope/activity",
    },
    {
      path: "/usage",
      name: "usage",
      component: UsageView,
      alias: "/s/:scope/usage",
    },
    {
      path: "/gateway",
      name: "gateway",
      component: GatewayView,
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
      component: SettingsView,
      meta: { focus: "settings" },
    },
    { path: "/:pathMatch(.*)*", redirect: "/overview" },
  ],
});

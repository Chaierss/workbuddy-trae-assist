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
      /** 与设置同构：壳层让出 240px，改渲染网关自己的左栏。 */
      meta: { immersive: "shell" },
    },
    /**
     * 设置中枢：`immersive: 'shell'` —— 壳层让出 240px，改渲染设置自己的左栏。
     * 形态必须存在 meta 里：存 store 或本地 state 会导致刷新后布局丢失。
     * 不设 permission（本项目暂无权限体系），将来权限下沉到每个 tab。
     *
     * 注意：`/docs` 不归前端 —— 那是后端自建的 Swagger 页，由接口文档 tab 用 iframe 内嵌。
     */
    {
      path: "/settings",
      name: "settings",
      component: SettingsView,
      meta: { immersive: "shell" },
    },
    { path: "/:pathMatch(.*)*", redirect: "/overview" },
  ],
});

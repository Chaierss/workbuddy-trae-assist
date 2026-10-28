<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterView, useRoute, useRouter } from "vue-router";
import CommandPalette from "./CommandPalette.vue";
import SidebarRail from "./SidebarRail.vue";
import SwitchDialog from "./SwitchDialog.vue";
import ToastHost from "./ToastHost.vue";
import UpdateDialog from "./UpdateDialog.vue";
import { closeSwitch, confirmSwitch, openSwitch, switchFlow } from "../lib/switchFlow";
import { initUpdateStore } from "../stores/update";
import { resolveFocusModule } from "../lib/focusMode";
import { NAV_DESTINATIONS } from "../lib/nav";
import {
  accounts,
  changeScope,
  clientDetections,
  isImplementedScope,
  scope,
  scopedPath,
} from "../stores/app";
import { api } from "../api";
import { desktop } from "../lib/desktop";
import { setMetricsEnabled, track } from "../lib/metrics";
import { ensureOnboarding } from "../lib/onboarding";

/**
 * 唯一的壳层，承载三种布局形态：
 *
 *   normal   普通页    全局侧栏 + 内容列常规滚动
 *   focus    专注模式  模块左栏替换全局侧栏；由 `meta.focus` 声明模块，
 *                     子页 ≥ 2 时自动启用（见 `lib/focusMode.ts`）
 *   inline   页面全屏自持（画布、看板编辑），无偏移、无内边距
 *
 * 布局差异只允许出现在下面三个 computed 里。**不要再建第二个 Layout**：
 * 那会让全局副作用挂载点翻倍，路由切换时整层卸载重建、过渡断帧。
 */
const route = useRoute();
const router = useRouter();
const navCollapsed = ref(false);
const paletteOpen = ref(false);

const focusModule = computed(() => resolveFocusModule(route.meta.focus));
const focusRail = computed(() => focusModule.value?.rail ?? null);
const inlinePage = computed(() => route.meta.immersive === "inline");

const layoutMode = computed<"normal" | "focus" | "inline">(() => {
  if (inlinePage.value) return "inline";
  return focusModule.value ? "focus" : "normal";
});

/** 收展动画的唯一正确写法：改这一个变量，侧栏宽度与内容偏移同时消费它。 */
const navRailVars = computed(() => ({
  "--nav-rail-current": navCollapsed.value
    ? "var(--nav-rail-width-collapsed)"
    : "var(--nav-rail-width)",
}));

const contentOffsetClass = computed(() => {
  if (layoutMode.value === "inline") return "";
  return layoutMode.value === "focus"
    ? "ml-[var(--sider-width)]"
    : "ml-[var(--nav-rail-current)]";
});

const mainClasses = computed(() => {
  if (layoutMode.value === "inline") return "min-h-0 flex-1 overflow-hidden p-0";
  // 专注模式的 main 必须 overflow-hidden，滚动交给页面自持，
  // 否则页面里固定在底部的输入框会被滚走。
  if (layoutMode.value === "focus") return "min-h-0 flex-1 overflow-hidden px-6 py-6";
  return "min-h-0 flex-1 overflow-y-auto px-8 py-7";
});

// ---------- 作用域写进 URL ----------

/**
 * 进 `/s/<scope>/…` 就把全局作用域切过去。不带前缀的短地址不改动作用域，
 * 作为回退路径保留（侧栏链接总是带前缀，见 lib/nav.ts + SidebarRail）。
 */
watch(
  () => route.params.scope,
  (raw) => {
    const key = typeof raw === "string" ? raw : "";
    if (!key || key === scope.value) return;
    if (!isImplementedScope(key)) return;
    void changeScope(key);
  },
  { immediate: true },
);

// ---------- 快捷键 ----------

/** 跳转的唯一写入口：命令面板与数字快捷键共用，保证都带上作用域前缀。 */
function goTo(path: string): void {
  void router.push(scopedPath(path));
}

/**
 * `Ctrl+K` 打开跳转面板，`Ctrl+1..6` 直达侧栏第 n 个目的地。
 *
 * 浏览器把 Ctrl+K（聚焦地址栏）与 Ctrl+1..8（切标签页）留给了自己，
 * 所以同时接受一组备用键 `Alt+K` / `Alt+1..6` —— 浏览器形态下用 Alt，
 * 桌面壳（PWA / 套壳）里 Ctrl 组合也不受限。
 */
function onShortcut(event: KeyboardEvent): void {
  if (event.metaKey) return;
  const useCtrl = event.ctrlKey && !event.altKey;
  const useAlt = event.altKey && !event.ctrlKey;
  if (!useCtrl && !useAlt) return;

  const key = event.key.toLowerCase();
  if (key === "k") {
    event.preventDefault();
    paletteOpen.value = true;
    return;
  }
  const matched = /^([1-6])$/.exec(key);
  if (!matched) return;
  const target = NAV_DESTINATIONS[Number(matched[1]) - 1];
  if (!target) return;
  event.preventDefault();
  goTo(target.path);
}

// ---------- 桌面托盘：账号摘要与换号 ----------

let unsubscribeTray: (() => void) | null = null;

/** 托盘选中态要的是账号 id；检测结果给的是 externalId，这里翻译一次。 */
const currentAccountId = computed(() => {
  const externalIds = clientDetections.value
    .map((client) => client.currentAccountId)
    .filter((id): id is string => Boolean(id));
  if (!externalIds.length) return "";
  return accounts.value.find((item) => externalIds.includes(item.externalId))?.id ?? "";
});

/**
 * 同步给托盘：最近使用的 8 个号，顺序与号池列表一致。
 * 只传 id / name / platform —— 凭据与路径一律不出渲染进程；浏览器形态没有托盘，整块跳过。
 */
watch(
  [accounts, currentAccountId],
  () => {
    desktop?.tray?.syncAccounts({
      items: [...accounts.value]
        .sort((a, b) => (b.lastUsedAt ?? "").localeCompare(a.lastUsedAt ?? ""))
        .slice(0, 8)
        .map((account) => ({
          id: account.id,
          name: account.displayName,
          platform: account.platform,
        })),
      currentId: currentAccountId.value,
    });
  },
  { immediate: true },
);

/** 托盘里点了某个号：走和页面完全相同的预检流程，不另开一条快捷路径。 */
function subscribeTraySwitch(): void {
  unsubscribeTray =
    desktop?.tray?.onSwitchAccount((id) => {
      const account = accounts.value.find((item) => item.id === id);
      // 清单可能已过期（号刚被删）：静默忽略，不弹一个点不动的错误。
      if (account) openSwitch(account, "tray");
    }) ?? null;
}

onMounted(() => {
  document.addEventListener("keydown", onShortcut);
  // 更新 store 要尽早初始化：先订阅主进程的 update:state / update:prompt，再拉初值，
  // 否则启动后那次静默检查推来的「新版本 / 已下载」会没人接。
  initUpdateStore();
  void initTelemetry();
  subscribeTraySwitch();
});
onBeforeUnmount(() => {
  document.removeEventListener("keydown", onShortcut);
  unsubscribeTray?.();
  unsubscribeTray = null;
});

// ---------- 本地体验数据 + 首启引导 ----------

const OPENED_KEY = "chaierww.opened";
/** 本次会话已上报过的客户端，避免检测轮询把它刷成几十条事件。 */
const reportedClients = new Set<string>();

async function initTelemetry(): Promise<void> {
  try {
    const prefs = await api.preferences();
    setMetricsEnabled(prefs.localMetricsEnabled);
  } catch {
    // 读不到偏好就先按默认开启处理；后端仍会按它自己的开关丢弃事件。
  }
  const first = !localStorage.getItem(OPENED_KEY);
  localStorage.setItem(OPENED_KEY, "1");
  track("app_open", { first });
  void ensureOnboarding();
}

/** 客户端检测结果变化时上报一次「装了 / 登了 / 在跑」。只有状态词，没有路径。 */
watch(
  clientDetections,
  (list) => {
    for (const client of list) {
      const status = !client.installed
        ? "missing"
        : client.loggedIn
          ? client.running
            ? "running"
            : "logged_in"
          : "installed";
      const key = `${client.scope}:${status}`;
      if (reportedClients.has(key)) continue;
      reportedClients.add(key);
      track("client_detected", { platform: client.scope, status });
    }
  },
  { immediate: true },
);
</script>

<template>
  <div class="app-canvas relative h-screen overflow-hidden" :style="navRailVars">
    <SidebarRail v-if="layoutMode === 'normal'" v-model:collapsed="navCollapsed" />
    <component :is="focusRail" v-else-if="layoutMode === 'focus' && focusRail" />

    <div
      class="flex h-full min-h-0 min-w-0 flex-col transition-[margin] duration-200 ease-out"
      :class="contentOffsetClass"
    >
      <main :class="mainClasses">
        <!-- 宽屏上限：内容列不跟着显示器无限拉宽，卡片和表格保持可读的行长。 -->
        <div class="mx-auto w-full" :class="layoutMode === 'normal' ? 'max-w-[1560px]' : 'h-full'">
          <RouterView v-slot="{ Component }">
            <Transition name="page" mode="out-in">
              <component :is="Component" />
            </Transition>
          </RouterView>
        </div>
      </main>
    </div>

    <CommandPalette v-if="paletteOpen" @close="paletteOpen = false" />
    <!-- 换号弹层挂在壳层：命令面板 / 托盘发起时，切到任何页面都不会丢流程 -->
    <SwitchDialog
      :open="switchFlow.open"
      :report="switchFlow.report"
      :busy="switchFlow.busy"
      :error="switchFlow.error"
      :preview="switchFlow.preview"
      :confirming="switchFlow.confirming"
      @close="closeSwitch"
      @confirm="confirmSwitch"
    />
    <!-- 更新弹窗挂在壳层：不依赖任何路由，主进程一推 update:prompt 就能弹 -->
    <UpdateDialog />
    <!-- 全局轻提示：所有页面的成功/失败反馈都走这一个容器 -->
    <ToastHost />
  </div>
</template>

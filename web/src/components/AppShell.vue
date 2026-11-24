<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterView, useRoute, useRouter } from "vue-router";
import { PanelLeft, Search } from "lucide-vue-next";
import CommandPalette from "./CommandPalette.vue";
import { checkForUpdates } from "../stores/update";
import SidebarRail from "./SidebarRail.vue";
import SwitchDialog from "./SwitchDialog.vue";
import TaskCenterDrawer from "./TaskCenterDrawer.vue";
import ToastHost from "./ToastHost.vue";
import UpdateDialog from "./UpdateDialog.vue";
import { closeSwitch, confirmSwitch, openSwitch, switchFlow } from "../lib/switchFlow";
import { initUpdateStore } from "../stores/update";
import { taskDrawerOpen } from "../stores/tasks";
import { resolveFocusModule } from "../lib/focusMode";
import { NAV_DESTINATIONS } from "../lib/nav";
import {
  accounts,
  changeScope,
  clientDetections,
  isImplementedScope,
  loadAccounts,
  loadCredentialHealth,
  resolvedTheme,
  scope,
  scopedPath,
} from "../stores/app";
import { api } from "../api";
import { customTitlebar, desktop } from "../lib/desktop";
import { setMetricsEnabled, track } from "../lib/metrics";
import { ensureOnboarding } from "../lib/onboarding";
import { backendOffline, resetHealthWatch } from "../lib/healthWatch";
import { t } from "../lib/i18n";

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
/** 菜单按钮 / 面板 / 菜单项的共用样式，避免四份菜单各写一遍。 */
const menuBtnClass =
  "flex h-[22px] items-center rounded-[6px] px-1 text-caption text-muted transition-colors hover:bg-surface hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/35";
const menuPanelClass =
  "absolute left-0 top-[calc(100%+4px)] z-[320] max-h-[70vh] w-60 overflow-y-auto rounded-control border border-line bg-surface py-1 shadow-float";
const menuItemClass =
  "flex w-full items-center justify-between gap-3 px-3 py-1.5 text-left text-body-sm text-ink-soft transition-colors hover:bg-line-soft hover:text-ink";

/** 快捷键键帽：每颗键单独一个圆角块，和参考里的 Ctrl + Z 一致。 */
const keycapClass =
  "inline-flex h-[18px] min-w-[19px] items-center justify-center rounded-[4px] border border-line bg-canvas px-1 text-micro font-medium text-muted";

const modifier = desktop?.platform === "darwin" ? "⌘" : "Ctrl";

interface MenuEntry {
  type?: "item" | "separator";
  label?: string;
  shortcut?: string[];
  action?: () => void;
}

interface MenuDef {
  key: MenuKey;
  label: string;
  mnemonic: string;
  items: MenuEntry[];
}

/** 撤销 / 剪切 / 复制这类文档动作走浏览器原生命令，不必回主进程。 */
function runEditCommand(command: "undo" | "redo" | "cut" | "copy" | "paste" | "selectAll"): void {
  document.execCommand(command);
}

/** 缩放 / 全屏 / 开发者工具 / 日志目录 / 关于 / 退出：这些只有主进程能做。 */
function runAppAction(action: string): void {
  void desktop?.appMenu?.run(action);
}

const menus = computed<MenuDef[]>(() => [
  {
    key: "file",
    label: t("shell.menuFile"),
    mnemonic: "F",
    items: [
      { label: t("nav.accounts"), action: () => goTo("/accounts") },
      {
        label: t("tasks.title"),
        action: () => {
          taskDrawerOpen.value = true;
        },
      },
      { label: t("nav.settings"), action: () => goTo("/settings") },
      { type: "separator" },
      { label: t("shell.quit"), action: () => runAppAction("quit") },
    ],
  },
  {
    key: "edit",
    label: t("shell.menuEdit"),
    mnemonic: "E",
    items: [
      { label: t("shell.undo"), shortcut: [modifier, "Z"], action: () => runEditCommand("undo") },
      { label: t("shell.redo"), shortcut: [modifier, "Y"], action: () => runEditCommand("redo") },
      { type: "separator" },
      { label: t("shell.cut"), shortcut: [modifier, "X"], action: () => runEditCommand("cut") },
      { label: t("shell.copy"), shortcut: [modifier, "C"], action: () => runEditCommand("copy") },
      { label: t("shell.paste"), shortcut: [modifier, "V"], action: () => runEditCommand("paste") },
      {
        label: t("shell.selectAll"),
        shortcut: [modifier, "A"],
        action: () => runEditCommand("selectAll"),
      },
    ],
  },
  {
    key: "view",
    label: t("shell.menuView"),
    mnemonic: "V",
    items: [
      {
        label: t("shell.fullscreen"),
        shortcut: ["F11"],
        action: () => runAppAction("fullscreen"),
      },
      { type: "separator" },
      { label: t("shell.zoomIn"), shortcut: [modifier, "="], action: () => runAppAction("zoom-in") },
      { label: t("shell.zoomOut"), shortcut: [modifier, "-"], action: () => runAppAction("zoom-out") },
      {
        label: t("shell.zoomReset"),
        shortcut: [modifier, "0"],
        action: () => runAppAction("zoom-reset"),
      },
    ],
  },
  {
    key: "help",
    label: t("shell.help"),
    mnemonic: "H",
    items: [
      {
        label: t("shell.devtools"),
        shortcut: [modifier, "Shift", "I"],
        action: () => runAppAction("devtools"),
      },
      { label: t("shell.openLogs"), action: () => runAppAction("open-logs") },
      { type: "separator" },
      ...(desktop
        ? [{ label: t("shell.checkUpdates"), action: () => void checkForUpdates() }]
        : []),
      { type: "separator" },
      { label: t("shell.about"), action: () => runAppAction("about") },
    ],
  },
]);

/** 顶栏菜单：一次只开一个，鼠标移到别的菜单上直接切换（和 VS Code 一致）。 */
type MenuKey = "file" | "edit" | "view" | "help";
const openMenu = ref<MenuKey | null>(null);

function closeMenu(): void {
  if (!openMenu.value) return;
  openMenu.value = null;
  window.removeEventListener("pointerdown", onMenuOutside, true);
  window.removeEventListener("keydown", onMenuKeydown, true);
}

function onMenuOutside(event: PointerEvent): void {
  const target = event.target;
  if (target instanceof Element && target.closest(".app-menu")) return;
  closeMenu();
}

function onMenuKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") closeMenu();
}

function toggleMenu(key: MenuKey): void {
  if (openMenu.value === key) {
    closeMenu();
    return;
  }
  if (!openMenu.value) {
    window.addEventListener("pointerdown", onMenuOutside, true);
    window.addEventListener("keydown", onMenuKeydown, true);
  }
  openMenu.value = key;
}

/** 已经开着菜单时，悬停到同排其它菜单直接切过去，不用先关再点。 */
function hoverMenu(key: MenuKey): void {
  if (openMenu.value && openMenu.value !== key) openMenu.value = key;
}

/** 菜单项触发前先收起，避免下拉压住后续页面 / 弹窗。 */
function runMenu(action: () => void): void {
  closeMenu();
  action();
}

const focusModule = computed(() => resolveFocusModule(route.meta.focus));
const focusRail = computed(() => focusModule.value?.rail ?? null);
const inlinePage = computed(() => route.meta.immersive === "inline");

const layoutMode = computed<"normal" | "focus" | "inline">(() => {
  if (inlinePage.value) return "inline";
  return focusModule.value ? "focus" : "normal";
});

/**
 * 自绘顶栏的设计高度。注意这里的单位是「设计像素」：
 * 页面缩放（Ctrl+= / Ctrl+-）会把 CSS 像素一起放大，顶栏若跟着长高，
 * 菜单会越缩越粗。所以顶栏内部按 1/缩放倍数 反向缩放，
 * 高度也按 `32 / 倍数` 换算，保证任何缩放级别下物理高度都是 32px。
 */
const TITLEBAR_BASE_HEIGHT = 32;
/** 页面缩放倍数（1 = 100%）。浏览器形态没有主进程，恒为 1。 */
const uiZoom = ref(1);
let unsubscribeZoom: (() => void) | null = null;

/** 收展动画的唯一正确写法：改这一个变量，侧栏宽度与内容偏移同时消费它。 */
const navRailVars = computed(() => ({
  // 侧栏宽度同样按 1/缩放倍数 折算：放大页面时侧栏不跟着变宽，
  // 内部再按 1/倍数 缩回设计尺寸（见 SidebarRail 的 railInnerStyle）。
  "--nav-rail-current": navCollapsed.value
    ? "calc(var(--nav-rail-width-collapsed) / var(--ui-zoom))"
    : "calc(var(--nav-rail-width) / var(--ui-zoom))",
  "--sider-current": "calc(var(--sider-width) / var(--ui-zoom))",
  // 桌面端自绘顶栏：单行 32px 设计高度，放折叠、搜索、菜单，右侧让给系统窗口按钮。
  "--titlebar-height": customTitlebar ? `${TITLEBAR_BASE_HEIGHT / uiZoom.value}px` : "0px",
  // 顶栏内容用它做反向缩放，缩放倍数变了只改这一个变量。
  "--ui-zoom": String(uiZoom.value),
  // 右上角原生窗口按钮的占位宽度（macOS 红绿灯在左边，占位为 0）。
  "--window-controls-width": customTitlebar && desktop?.platform !== "darwin" ? "138px" : "0px",
}));

/**
 * macOS 的红绿灯在左上角，顶栏内容要让开这段系统区域；
 * Windows 右侧给原生窗口按钮留出覆盖层宽度。
 */
const titlebarPadding = {
  paddingLeft: desktop?.platform === "darwin" ? "86px" : "0px",
  paddingRight: "calc(var(--window-controls-width) + 4px)",
};

/**
 * 顶栏内容的反向缩放：外层按 `32 / 倍数` 留出高度，内层先按倍数撑宽再用 1/倍数 缩回，
 * 结果就是内容始终以设计像素呈现，页面放大时顶栏不会被一起吹大。
 */
const titlebarScale = {
  width: "calc(100% * var(--ui-zoom))",
  transform: "scale(calc(1 / var(--ui-zoom)))",
  transformOrigin: "top left",
};

/** 命令面板的快捷键提示：帮助菜单和侧栏用同一份，别一处 ⌘K 一处 Ctrl K。 */
const paletteShortcut = desktop?.platform === "darwin" ? "⌘K" : "Ctrl K";

/**
 * 自绘标题栏里原生窗口按钮的配色跟随主题。
 *
 * 主题只会写到 `document.documentElement[data-theme]`（见 stores/app.ts 的
 * applyTheme），这里用 MutationObserver 监听这个属性，而不是再复制一份
 * 「浅色 / 深色 / 跟随系统」的判断逻辑——主题只有一个真相源。
 */
let themeObserver: MutationObserver | null = null;

function syncTitlebarTheme(): void {
  desktop?.chrome?.setTheme(resolvedTheme() === "dark");
}

const contentOffsetClass = computed(() => {
  if (layoutMode.value === "inline") return "";
  return layoutMode.value === "focus"
    ? "ml-[var(--sider-current)]"
    : "ml-[var(--nav-rail-current)]";
});

const mainClasses = computed(() => {
  if (layoutMode.value === "inline") return "min-h-0 flex-1 overflow-hidden bg-canvas p-0";
  // 专注模式的 main 必须 overflow-hidden，滚动交给页面自持，
  // 否则页面里固定在底部的输入框会被滚走。
  if (layoutMode.value === "focus") return "min-h-0 flex-1 overflow-hidden bg-canvas px-6 py-6";
  // 常规布局：顶栏、侧栏、内容共用同一张画布底色，层次只交给卡片与留白，
  // 左边不再留页边距，卡片直接贴在侧栏边缘，右侧保留呼吸位。
  return "min-h-0 flex-1 overflow-y-auto bg-canvas pl-0 pr-3 py-6";
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
 * `Ctrl+K` 打开跳转面板，`Ctrl+,` 打开设置，`Ctrl+1..6` 直达侧栏第 n 个目的地。
 *
 * 浏览器把 Ctrl+K（聚焦地址栏）与 Ctrl+1..8（切标签页）留给了自己，
 * 所以同时接受一组备用键 `Alt+K` / `Alt+,` / `Alt+1..6` —— 浏览器形态下用 Alt，
 * 桌面壳（PWA / 套壳）里 Ctrl 组合也不受限。
 */
function onShortcut(event: KeyboardEvent): void {
  // F11 全屏：Electron 没有原生菜单后不会被自动接管，这里补上。
  if (event.key === "F11" && customTitlebar) {
    event.preventDefault();
    runAppAction("fullscreen");
    return;
  }
  if (event.metaKey) return;
  const useCtrl = event.ctrlKey && !event.altKey;
  const useAlt = event.altKey && !event.ctrlKey;
  if (!useCtrl && !useAlt) return;

  const key = event.key.toLowerCase();
  // 缩放与开发者工具：和「查看」「帮助」菜单里标注的快捷键保持一致。
  if (useCtrl && customTitlebar) {
    if (key === "=" || key === "+") {
      event.preventDefault();
      runAppAction("zoom-in");
      return;
    }
    if (key === "-") {
      event.preventDefault();
      runAppAction("zoom-out");
      return;
    }
    if (key === "0") {
      event.preventDefault();
      runAppAction("zoom-reset");
      return;
    }
    if (key === "i" && event.shiftKey) {
      event.preventDefault();
      runAppAction("devtools");
      return;
    }
  }
  // Alt+F/E/V/H：与顶栏菜单标题里的助记符一致；浏览器形态没有这条菜单，不抢键。
  if (useAlt && customTitlebar) {
    const menuKeys: Record<string, MenuKey> = { f: "file", e: "edit", v: "view", h: "help" };
    const menu = menuKeys[key];
    if (menu) {
      event.preventDefault();
      toggleMenu(menu);
      return;
    }
  }
  if (key === "k") {
    event.preventDefault();
    paletteOpen.value = true;
    return;
  }
  if (key === ",") {
    event.preventDefault();
    goTo("/settings");
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
let unsubscribeTrayFocus: (() => void) | null = null;

/**
 * 后台凭据巡检间隔。托盘常驻时页面可能停在别的模块，没人轮询账号列表，
 * 「凭据异常 → 系统通知」就会等到用户打开账号页才触发。5 分钟拉一次本地接口。
 */
const BACKGROUND_REFRESH_MS = 5 * 60 * 1000;
let backgroundRefresh: number | null = null;

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
 * 只传 id / name / platform / needsAttention —— 凭据与路径一律不出渲染进程；
 * 浏览器形态没有托盘，整块跳过。
 */
const attentionAccounts = computed(() =>
  accounts.value.filter(
    (account) =>
      account.credentialState === "relogin_required" ||
      account.credentialAutoPaused ||
      Boolean(account.credentialErrorCode),
  ),
);

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
          needsAttention:
            account.credentialState === "relogin_required" ||
            account.credentialAutoPaused ||
            Boolean(account.credentialErrorCode),
        })),
      currentId: currentAccountId.value,
    });
  },
  { immediate: true },
);

/** 凭据出问题的号：交给主进程弹系统通知（每号每天最多一次）。 */
watch(
  attentionAccounts,
  (items) => {
    if (!items.length) return;
    desktop?.health?.notify({
      items: items.map((account) => ({
        id: account.id,
        name: account.displayName,
      })),
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
  unsubscribeTrayFocus =
    desktop?.tray?.onFocusCredentialHealth(() => {
      void router.push(scopedPath("/accounts"));
    }) ?? null;
}

/**
 * 成功换号后点「切回上一个号」：重新走完整预检，不绕过确认步骤。
 * `openSwitch` 会把「切换前账号」重新记成刚切到的号，连续切换也不会串。
 */
function switchBack(): void {
  const previous = switchFlow.previous;
  if (previous) void openSwitch(previous, "manual");
}

onMounted(() => {
  document.addEventListener("keydown", onShortcut);
  syncTitlebarTheme();
  themeObserver = new MutationObserver(syncTitlebarTheme);
  themeObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
  // 更新 store 要尽早初始化：先订阅主进程的 update:state / update:prompt，再拉初值，
  // 否则启动后那次静默检查推来的「新版本 / 已下载」会没人接。
  initUpdateStore();
  void initTelemetry();
  subscribeTraySwitch();
  // 顶栏高度与页面缩放反向挂钩：先取一次初值，再订阅主进程推送（菜单缩放 + Ctrl 滚轮）。
  if (customTitlebar) {
    const chrome = desktop?.chrome;
    if (chrome?.getZoom) {
      void chrome
        .getZoom()
        .then((factor) => {
          if (factor > 0) uiZoom.value = factor;
        })
        .catch(() => {});
    }
    unsubscribeZoom =
      chrome?.onZoom?.((factor) => {
        if (factor > 0) uiZoom.value = factor;
      }) ?? null;
  }
  // 只有桌面端有系统通知；浏览器形态不做这次轮询。
  if (desktop) {
    backgroundRefresh = window.setInterval(() => {
      // 凭据体检跟着账号列表一起拉：否则后台换了登录态，
      // 号池的「凭据不完整」提示会一直停在旧的结论上。
      void Promise.all([loadAccounts(), loadCredentialHealth()]).catch(() => {});
    }, BACKGROUND_REFRESH_MS);
  }
});
onBeforeUnmount(() => {
  document.removeEventListener("keydown", onShortcut);
  closeMenu();
  themeObserver?.disconnect();
  themeObserver = null;
  unsubscribeTray?.();
  unsubscribeTray = null;
  unsubscribeTrayFocus?.();
  unsubscribeTrayFocus = null;
  unsubscribeZoom?.();
  unsubscribeZoom = null;
  if (backgroundRefresh !== null) {
    window.clearInterval(backgroundRefresh);
    backgroundRefresh = null;
  }
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

/** 断线横幅上的「重新连接」：先清计数再整页刷新，避免旧请求把状态又拉回离线。 */
function retryBackend(): void {
  resetHealthWatch();
  window.location.reload();
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
  <div
    class="app-canvas relative h-screen overflow-hidden"
    :style="navRailVars"
  >
    <!--
      自绘顶栏（32px 单行，页面缩放时高度不变）：左折叠 / 搜索 + 文件·编辑·查看·帮助 菜单，
      末端是系统窗口按钮的预留位。品牌与版本在侧栏顶部，不在这里重复。
    -->
    <header
      v-if="customTitlebar"
      class="app-titlebar fixed inset-x-0 top-0 z-[300] h-[var(--titlebar-height)]"
    >
      <!-- 内层按 1/缩放倍数 反向缩放：页面放大时菜单条保持 32px，不跟着变高变粗 -->
      <div
        class="flex h-8 min-w-0 items-center gap-0.5 pl-2"
        :style="[titlebarPadding, titlebarScale]"
      >
        <button
          type="button"
          class="grid size-[22px] shrink-0 place-items-center rounded-[6px] text-muted transition-colors hover:bg-surface hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/35"
          :title="navCollapsed ? t('shell.expandRail') : t('shell.collapseRail')"
          @click="navCollapsed = !navCollapsed"
        >
          <PanelLeft :size="14" :stroke-width="1.9" />
        </button>
        <button
          type="button"
          class="grid size-[22px] shrink-0 place-items-center rounded-[6px] text-muted transition-colors hover:bg-surface hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/35"
          :title="`${t('shell.commandHint')} · ${paletteShortcut}`"
          @click="paletteOpen = true"
        >
          <Search :size="14" :stroke-width="1.9" />
        </button>

        <span class="w-2 shrink-0" aria-hidden="true" />

        <div v-for="menu in menus" :key="menu.key" class="app-menu relative">
          <button
            type="button"
            :class="[menuBtnClass, openMenu === menu.key ? 'bg-surface text-ink' : '']"
            :aria-expanded="openMenu === menu.key"
            :title="`${menu.label} · Alt+${menu.mnemonic}`"
            @click="toggleMenu(menu.key)"
            @mouseenter="hoverMenu(menu.key)"
          >
            {{ menu.label }}
          </button>
          <div v-if="openMenu === menu.key" role="menu" :class="menuPanelClass">
            <template v-for="(entry, index) in menu.items" :key="index">
              <div v-if="entry.type === 'separator'" class="my-1 h-px bg-line-soft" />
              <button
                v-else
                type="button"
                role="menuitem"
                :class="menuItemClass"
                @mousedown.prevent
                @click="runMenu(() => entry.action?.())"
              >
                <span class="truncate">{{ entry.label }}</span>
                <span v-if="entry.shortcut?.length" class="flex shrink-0 items-center gap-0.5">
                  <template v-for="(keyName, keyIndex) in entry.shortcut" :key="keyIndex">
                    <span v-if="keyIndex" class="text-micro text-muted">+</span>
                    <kbd :class="keycapClass">{{ keyName }}</kbd>
                  </template>
                </span>
              </button>
            </template>
          </div>
        </div>

        <!-- 右侧整段留白：窗口拖拽区，末端是系统窗口按钮的预留位 -->
        <div class="h-full min-w-0 flex-1" aria-hidden="true" />
      </div>
    </header>

    <div
      v-if="backendOffline"
      role="status"
      class="absolute inset-x-0 top-[var(--titlebar-height)] z-50 flex flex-wrap items-center justify-center gap-3 bg-warn-soft px-4 py-2 text-body-sm text-warn shadow-card"
    >
      <span>{{ t("app.offline") }}</span>
      <button
        type="button"
        class="rounded-control border border-warn/30 px-2.5 py-1 text-caption font-medium text-warn transition-colors hover:bg-surface"
        @click="retryBackend"
      >
        {{ t("app.offlineRetry") }}
      </button>
    </div>
    <SidebarRail v-if="layoutMode === 'normal'" v-model:collapsed="navCollapsed" />
    <component :is="focusRail" v-else-if="layoutMode === 'focus' && focusRail" />

    <div
      class="flex h-full min-h-0 min-w-0 flex-col transition-[margin] duration-200 ease-out"
      :class="contentOffsetClass"
      :style="{ paddingTop: 'var(--titlebar-height)' }"
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

    <!-- 浏览器形态没有顶栏，命令面板是居中弹层；桌面端也从标题条的搜索图标打开 -->
    <CommandPalette v-if="paletteOpen" @close="paletteOpen = false" />
    <!-- 任务中心：复检 / 续期 / 备份 / 跑活动的结果都留在这里，刷新后仍可回看 -->
    <TaskCenterDrawer v-if="taskDrawerOpen" />
    <!-- 换号弹层挂在壳层：命令面板 / 托盘发起时，切到任何页面都不会丢流程 -->
    <SwitchDialog
      :open="switchFlow.open"
      :report="switchFlow.report"
      :busy="switchFlow.busy"
      :error="switchFlow.error"
      :preview="switchFlow.preview"
      :confirming="switchFlow.confirming"
      :previous="switchFlow.previous"
      @close="closeSwitch"
      @confirm="confirmSwitch"
      @switch-back="switchBack"
    />
    <!-- 更新弹窗挂在壳层：不依赖任何路由，主进程一推 update:prompt 就能弹 -->
    <UpdateDialog />
    <!-- 全局轻提示：所有页面的成功/失败反馈都走这一个容器 -->
    <ToastHost />
  </div>
</template>

<style scoped>
/* 自绘标题栏：整条可拖动窗口，交互控件必须退出拖拽区，否则点不动。
   双击拖拽区由系统处理最大化，不在前端再记一次。 */
.app-titlebar {
  -webkit-app-region: drag;
  user-select: none;
}

.app-titlebar :deep(button),
.app-titlebar :deep(a),
.app-titlebar :deep(input),
.app-titlebar :deep([role="button"]),
.app-titlebar :deep([role="menu"]) {
  -webkit-app-region: no-drag;
}
</style>

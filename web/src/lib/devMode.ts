import { isDesktop } from "./desktop";

/**
 * 开发模式 / 线上模式 —— 全项目**唯一**的判定入口。
 *
 * 铁律：只有这个文件读构建期变量；业务组件一律只调 `isDev()` / `canShow()`。
 * 判定点一旦散落，切线上时必然漏掉第七处。
 *
 * 三种来源的优先级：URL > 本地存储 > 构建期常量。
 * 线上包默认就是线上形态；本机（或桌面壳）允许临时升权到开发形态，
 * 用于交付前自查，角标会变成醒目的黄色并提示「看完请切回」。
 */

export type AppMode = "dev" | "prod";
export type ModeSource = "build" | "url" | "storage" | "default";

export interface ModeState {
  mode: AppMode;
  /** 结论从哪来 —— 角标 hover 要展示，排障时最关键。 */
  source: ModeSource;
  /** 线上包被临时升权到开发形态。 */
  elevated: boolean;
}

const STORAGE_KEY = "chaierww.appMode";
const URL_KEY = "mode";

/**
 * 构建期常量：显式 `VITE_APP_MODE` 优先；否则按构建方式取默认 ——
 * `vite build`（发布包、桌面安装包）是线上形态，`vite dev` 是开发形态。
 * 不能只认环境变量：打包流程不会额外设置它，缺省会把自己发成开发模式。
 */
const FORCED_MODE = import.meta.env.VITE_APP_MODE;
const BUILD_MODE: AppMode =
  FORCED_MODE === "prod" ? "prod" : FORCED_MODE === "dev" ? "dev" : import.meta.env.PROD ? "prod" : "dev";

/**
 * 谁能把线上包升权到开发形态。
 *
 * 本项目只监听 127.0.0.1 且没有 token 鉴权：能打开这个页面的人本来就拥有
 * 全部权限，「管理员」在这里等价于本机访问 / 桌面壳。远端来源一律拒绝，
 * 避免 `?mode=dev` 变成信息泄露入口。
 */
function canElevate(): boolean {
  if (isDesktop) return true;
  if (typeof window === "undefined") return false;
  const host = window.location.hostname;
  return host === "localhost" || host === "127.0.0.1" || host === "::1" || host === "[::1]";
}

function readUrlMode(): AppMode | null {
  if (typeof window === "undefined") return null;
  const value = new URLSearchParams(window.location.search).get(URL_KEY);
  return value === "dev" || value === "prod" ? value : null;
}

function readStoredMode(): AppMode | null {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    return value === "dev" || value === "prod" ? value : null;
  } catch {
    return null;
  }
}

function resolve(mode: AppMode | null, source: ModeSource): ModeState {
  if (mode === null) return { mode: BUILD_MODE, source: BUILD_MODE === "dev" ? "default" : "build", elevated: false };
  // 线上包 + 非本机 → 直接忽略，不提示，避免被探测。
  if (mode === "dev" && BUILD_MODE === "prod" && !canElevate()) {
    return { mode: BUILD_MODE, source: "build", elevated: false };
  }
  return { mode, source, elevated: mode === "dev" && BUILD_MODE === "prod" };
}

export function resolveMode(): ModeState {
  const urlMode = readUrlMode();
  if (urlMode) return resolve(urlMode, "url");
  const stored = readStoredMode();
  if (stored) return resolve(stored, "storage");
  return resolve(null, "build");
}

/** 全项目唯一的布尔出口。业务代码只认它，不读 `import.meta.env`。 */
export function isDev(): boolean {
  return resolveMode().mode === "dev";
}

/**
 * 切换模式。必须整页重载 —— 半开发半线上的界面比纯线上更危险。
 * 返回 false 表示被拒（远端访问时不允许升权）。
 */
export function setMode(mode: AppMode): boolean {
  if (mode === "dev" && BUILD_MODE === "prod" && !canElevate()) return false;
  try {
    window.localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    return false;
  }
  window.location.reload();
  return true;
}

/** 清掉覆盖，回到构建期形态。 */
export function resetMode(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* ignore */
  }
  window.location.reload();
}

/**
 * 可见性矩阵：新增内部事实（告警条、源码路径、试调入口）时先在这里填一行，
 * 页面只调 `canShow()`，不要临场判断。
 */
export const MODE_VISIBILITY = {
  /** 规则出处、术语表 —— 内部设计语言，不给线上用户看。 */
  rulesInternal: { dev: true, prod: false },
  /** 组件源码路径。 */
  componentSourcePath: { dev: true, prod: false },
  /** 未登记 / 未引用组件的告警条。 */
  componentDebt: { dev: true, prod: false },
  /** Wiki 正文的仓库路径。 */
  wikiSourcePath: { dev: true, prod: false },
  /** 接口试调入口。 */
  apiTryItOut: { dev: true, prod: false },
  /** 原始错误堆栈。 */
  rawErrorStack: { dev: true, prod: false },
} as const satisfies Record<string, { dev: boolean; prod: boolean }>;

export function canShow(key: keyof typeof MODE_VISIBILITY): boolean {
  return MODE_VISIBILITY[key][resolveMode().mode];
}

/**
 * 桌面壳（Electron）桥接层。
 *
 * 同一个前端产物既跑在浏览器里（`python -m chaierww.main` 直接打开），
 * 也跑在桌面端（Electron 内嵌）。两者能力不同：
 *
 * - 桌面端能检查更新（需要主进程）
 * - 浏览器端能开接口文档 / Wiki（桌面壳里那两个页面是 iframe 套文档，
 *   体验比浏览器差，所以在桌面端隐藏）
 *
 * 判断依据只有一条：preload 有没有注入 `window.chaierwwDesktop`。
 * 不靠 UA、不靠 URL 参数——那两种都能被误判。
 */

export type DesktopUpdateStatus =
  | "idle"
  | "disabled"
  | "checking"
  | "up-to-date"
  | "available"
  | "downloading"
  | "downloaded"
  | "error";

export interface DesktopUpdateState {
  status: DesktopUpdateStatus;
  currentVersion: string;
  /** 有新版本时才有值。 */
  version: string | null;
  releaseNotes: string;
  /** 0–100，仅在 downloading / downloaded 有意义。 */
  percent: number;
  /** status 为 error 时的原因。 */
  message: string;
  checkedAt: string | null;
}

/** 更新偏好，存在主进程的 userData/update-prefs.json。 */
export interface DesktopUpdatePrefs {
  /** 启动 8 秒后是否自动检查；关掉后只有手动点「检查更新」才查。 */
  autoCheck: boolean;
}

export interface DesktopBridge {
  isDesktop: true;
  version: string;
  platform: string;
  update: {
    get: () => Promise<DesktopUpdateState>;
    check: () => Promise<DesktopUpdateState>;
    download: () => Promise<DesktopUpdateState>;
    install: () => Promise<boolean>;
    getPrefs: () => Promise<DesktopUpdatePrefs>;
    setPrefs: (patch: Partial<DesktopUpdatePrefs>) => Promise<DesktopUpdatePrefs>;
    /** 返回取消订阅函数，组件卸载时必须调用。 */
    onState: (handler: (state: DesktopUpdateState) => void) => () => void;
  };
}

declare global {
  interface Window {
    chaierwwDesktop?: DesktopBridge;
  }
}

/** 桌面壳桥接对象；浏览器里为 null。 */
export const desktop: DesktopBridge | null =
  typeof window === "undefined" ? null : (window.chaierwwDesktop ?? null);

/** 当前是否运行在桌面端。整个前端只有这一个判断入口。 */
export const isDesktop: boolean = desktop !== null;

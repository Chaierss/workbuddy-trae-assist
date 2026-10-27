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

/** 主进程把原始错误归类后的原因，渲染层据此挑文案；原始 message 只放「详情」。 */
export type DesktopUpdateErrorKind =
  | ""
  | "network"
  | "noRelease"
  | "download"
  | "checksum"
  | "disk"
  | "unknown";

export interface DesktopUpdateState {
  status: DesktopUpdateStatus;
  currentVersion: string;
  /** 有新版本时才有值。 */
  version: string | null;
  releaseNotes: string;
  /** 0–100，仅在 downloading / downloaded 有意义。 */
  percent: number;
  /** 已下载字节数，downloading 期间有值。 */
  transferred: number;
  /** 总字节数，downloading 期间有值（0 = 未知）。 */
  total: number;
  /** 瞬时速率（字节/秒），downloading 期间有值。 */
  bytesPerSecond: number;
  /** 归类后的错误原因。 */
  errorKind: DesktopUpdateErrorKind;
  /** status 为 error 时的原始原因（英文，可能含 URL）。 */
  message: string;
  checkedAt: string | null;
}

/** 更新偏好，存在主进程的 userData/update-prefs.json。 */
export interface DesktopUpdatePrefs {
  /** 启动后是否自动检查并静默下载；关掉后只有手动点「检查更新」才查。 */
  autoCheck: boolean;
  /** 用户主动跳过的版本号；null = 没跳过。 */
  skippedVersion: string | null;
}

export interface DesktopBridge {
  isDesktop: true;
  version: string;
  platform: string;
  /** 备份恢复后由主进程重启应用；浏览器端没有此能力。 */
  relaunch?: () => Promise<boolean>;
  update: {
    get: () => Promise<DesktopUpdateState>;
    check: () => Promise<DesktopUpdateState>;
    download: () => Promise<DesktopUpdateState>;
    install: () => Promise<boolean>;
    /** 跳过某个版本；传 null 可取消跳过。 */
    skip: (version: string | null) => Promise<DesktopUpdatePrefs>;
    unskip: () => Promise<DesktopUpdatePrefs>;
    getPrefs: () => Promise<DesktopUpdatePrefs>;
    setPrefs: (patch: Partial<DesktopUpdatePrefs>) => Promise<DesktopUpdatePrefs>;
    /** 返回取消订阅函数，组件卸载时必须调用。 */
    onState: (handler: (state: DesktopUpdateState) => void) => () => void;
    /** 订阅「该弹更新弹窗」事件，返回取消订阅函数。 */
    onPrompt: (handler: (payload: { kind: string }) => void) => () => void;
  };
  /** 内嵌登录窗口：桌面端把官方授权页放进自己的窗口，浏览器端为 null。 */
  webLogin: {
    open: (url: string) => Promise<{ ok: boolean; error?: string }>;
    close: () => Promise<boolean>;
    /** 打开内置 WorkBuddy 网页版，登录态独立保存，可手动做活动。 */
    openApp: () => Promise<{ ok: boolean; error?: string }>;
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

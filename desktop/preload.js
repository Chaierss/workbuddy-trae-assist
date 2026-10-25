/**
 * 预加载脚本：暴露最小化、安全的桥接能力给渲染进程。
 *
 * 前端只靠 `window.chaierwwDesktop` 判断「我在桌面壳里」，据此决定
 * 要不要显示「更新」设置页、要不要隐藏接口文档与 Wiki 这类浏览器页面。
 * 这里只放必要的几样能力，不开 Node 集成。
 */
const { contextBridge, ipcRenderer } = require('electron');

/**
 * 版本号从启动参数读，主进程在 `additionalArguments` 里塞进来的。
 *
 * 不能用 `require('electron').app.getVersion()`：`app` 只存在于主进程，
 * preload 跑在渲染进程里拿到的 `app` 是 `undefined`，调用即抛错。
 * 而 preload 一旦抛错，`exposeInMainWorld` 就不会执行，前端会把自己当成
 * 浏览器（`isDesktop === false`），桌面能力全部失效。
 */
function readVersion() {
  const prefix = '--chaierww-version=';
  const hit = process.argv.find((arg) => arg.startsWith(prefix));
  return hit ? hit.slice(prefix.length) : 'unknown';
}

contextBridge.exposeInMainWorld('chaierwwDesktop', {
  /** 显式标记：渲染进程据此区分「桌面端」与「浏览器里打开」。 */
  isDesktop: true,
  version: readVersion(),
  platform: process.platform,

  /** 更新相关：check 会连更新源，get 只读当前状态。 */
  update: {
    get: () => ipcRenderer.invoke('update:get'),
    check: () => ipcRenderer.invoke('update:check'),
    download: () => ipcRenderer.invoke('update:download'),
    install: () => ipcRenderer.invoke('update:install'),
    /** 跳过当前这个版本：自动检查不再提示它。传 null 可取消跳过。 */
    skip: (version) => ipcRenderer.invoke('update:skip', version),
    unskip: () => ipcRenderer.invoke('update:unskip'),
    /** 更新偏好（autoCheck：启动时是否自动检查；skippedVersion：跳过的版本）。 */
    getPrefs: () => ipcRenderer.invoke('update:get-prefs'),
    setPrefs: (patch) => ipcRenderer.invoke('update:set-prefs', patch),
    /**
     * 订阅主进程推来的状态。返回取消订阅函数——组件卸载时必须调用，
     * 否则窗口里来回切几次 tab 就会挂上一堆监听。
     */
    onState: (handler) => {
      const listener = (_event, state) => handler(state);
      ipcRenderer.on('update:state', listener);
      return () => ipcRenderer.removeListener('update:state', listener);
    },
    /**
     * 订阅「该弹更新弹窗」事件。与 onState 同构，返回取消订阅函数。
     * 主进程只在窗口可见时才推这个事件；窗口在托盘里只更新 tooltip。
     */
    onPrompt: (handler) => {
      const listener = (_event, payload) => handler(payload);
      ipcRenderer.on('update:prompt', listener);
      return () => ipcRenderer.removeListener('update:prompt', listener);
    },
  },

  /**
   * 内嵌登录窗口：把官方授权页放进我们自己的窗口里，用户不离开应用就能登录。
   * 只接受 WorkBuddy / CodeBuddy 的登录地址，主进程会再校验一次。
   */
  webLogin: {
    open: (url) => ipcRenderer.invoke('web-login:open', url),
    close: () => ipcRenderer.invoke('web-login:close'),
    /** 打开内置 WorkBuddy 网页版，登录态独立保存，可手动做活动。 */
    openApp: () => ipcRenderer.invoke('web-login:open-app'),
  },
});

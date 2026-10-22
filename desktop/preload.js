/**
 * 预加载脚本：暴露最小化、安全的桥接能力给渲染进程。
 *
 * 前端只靠 `window.chaierwwDesktop` 判断「我在桌面壳里」，据此决定
 * 要不要显示「更新」设置页、要不要隐藏接口文档与 Wiki 这类浏览器页面。
 * 这里只放必要的几样能力，不开 Node 集成。
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('chaierwwDesktop', {
  /** 显式标记：渲染进程据此区分「桌面端」与「浏览器里打开」。 */
  isDesktop: true,
  version: require('electron').app.getVersion(),
  platform: process.platform,

  /** 更新相关：check 会连更新源，get 只读当前状态。 */
  update: {
    get: () => ipcRenderer.invoke('update:get'),
    check: () => ipcRenderer.invoke('update:check'),
    download: () => ipcRenderer.invoke('update:download'),
    install: () => ipcRenderer.invoke('update:install'),
    /**
     * 订阅主进程推来的状态。返回取消订阅函数——组件卸载时必须调用，
     * 否则窗口里来回切几次 tab 就会挂上一堆监听。
     */
    onState: (handler) => {
      const listener = (_event, state) => handler(state);
      ipcRenderer.on('update:state', listener);
      return () => ipcRenderer.removeListener('update:state', listener);
    },
  },
});

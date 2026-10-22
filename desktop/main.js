/**
 * chaierww 桌面端主进程。
 *
 * 职责：
 *  1. 启动/关闭内嵌的 FastAPI 后端（PyInstaller 打包的独立可执行文件）
 *  2. 创建主窗口并加载 http://127.0.0.1:57910（后端托管的 SPA）
 *  3. 单实例锁、托盘、退出时回收后端进程
 *  4. 通过 electron-updater 检测更新，并把更新状态同步给渲染进程
 *
 * 平台差异集中在这里处理：
 *  - 后端可执行文件名（Windows 带 .exe）
 *  - 托盘图标（Windows 多尺寸 ico / macOS 模板图 / Linux png）
 *  - macOS 关窗不退出、Dock 点击唤起
 */
const { app, BrowserWindow, Menu, Tray, shell, dialog, ipcMain, nativeImage, nativeTheme } = require('electron');
const { autoUpdater } = require('electron-updater');
const { spawn, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const net = require('net');

const BACKEND_HOST = '127.0.0.1';
const BACKEND_PORT = 57910;
const APP_URL = `http://${BACKEND_HOST}:${BACKEND_PORT}/`;

const IS_WIN = process.platform === 'win32';
const IS_MAC = process.platform === 'darwin';

// GPU 兼容性说明：部分老显卡驱动（如 AMD Radeon 停更驱动）会让 Electron 的
// 独立 GPU 进程在极早期访问冲突崩溃（exit_code=-1073741819），最终 "GPU process
// isn't usable" 拖垮整个应用。解法是启动参数 --no-sandbox --in-process-gpu，让
// GPU 逻辑跑在主进程内、不走独立 GPU 进程（见 package.json 的 start 脚本）。
// 这里再追加一次命令行开关作为双保险，并关闭硬件加速。
app.commandLine.appendSwitch('no-sandbox');
app.commandLine.appendSwitch('in-process-gpu');
app.disableHardwareAcceleration();

// Windows 上不设 AppUserModelID 的话，任务栏会把窗口和托盘图标认成两个不同程序，
// 通知也挂不到应用上。
if (IS_WIN) app.setAppUserModelId('com.chaierss.chaierww');

// 后端是否由本进程拉起（用于退出时决定要不要一并回收）。
let backendProcess = null;
let mainWindow = null;
let tray = null;
let quitting = false;

// ---------------------------------------------------------------------------
// 单实例锁
// ---------------------------------------------------------------------------
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

// ---------------------------------------------------------------------------
// 后端定位与启动
// ---------------------------------------------------------------------------
function backendExecutable() {
  if (app.isPackaged) {
    // 打包后：extraResources 把 backend 放进 resources/backend
    const name = IS_WIN ? 'chaierww-backend.exe' : 'chaierww-backend';
    const candidate = path.join(process.resourcesPath, 'backend', name);
    return fs.existsSync(candidate) ? candidate : null;
  }
  // 开发模式：直接跑源码入口（用系统 python + 项目 venv）
  return null;
}

function webuiDir() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'webui');
  }
  // 开发模式：指向仓库的 web/dist
  return path.join(__dirname, '..', 'web', 'dist');
}

function isPortOpen(port) {
  return new Promise((resolve) => {
    const socket = net.connect(port, BACKEND_HOST);
    socket.once('connect', () => { socket.destroy(); resolve(true); });
    socket.once('error', () => resolve(false));
  });
}

function startBackend() {
  const webui = webuiDir();
  const env = {
    ...process.env,
    CHAIERWW_WEBUI: webui,
    // 后端会盯着这个 PID：主进程一旦没了（被安装程序强杀、崩溃），
    // 后端自己退出，否则它会一直占着 57910 和安装目录里的 exe，
    // 导致重装时提示「chaierww 无法关闭」。
    CHAIERWW_PARENT_PID: String(process.pid),
  };

  const exe = backendExecutable();
  if (exe) {
    // windowsHide：控制台子系统程序默认会弹一个黑框，这里必须藏掉。
    backendProcess = spawn(exe, [], { env, stdio: 'ignore', windowsHide: true });
  } else {
    // 开发模式：python -m chaierww.main
    // detached：起独立进程组，退出时能整组干掉（后端可能还有子进程）。
    const python = process.env.CHAIERWW_PYTHON || 'python';
    backendProcess = spawn(python, ['-m', 'chaierww.main'], {
      env,
      cwd: path.join(__dirname, '..'),
      stdio: 'ignore',
      windowsHide: true,
      detached: !IS_WIN,
    });
  }
  backendProcess.on('exit', (code) => {
    if (!quitting && code !== 0) {
      // 后端意外退出（非正常关闭），提示用户。
      dialog.showErrorBox(
        'chaierww',
        `后端服务已意外退出（退出码 ${code}）。请检查日志后重启应用。`
      );
    }
    backendProcess = null;
  });
}

async function waitForBackend(timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await isPortOpen(BACKEND_PORT)) return true;
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

function stopBackend() {
  if (!backendProcess) return;
  const child = backendProcess;
  backendProcess = null;
  const pid = child.pid;
  if (!pid) return;
  try {
    if (IS_WIN) {
      // 只 kill 父进程没用：PyInstaller 的 --onefile 是「引导器 + 真正干活的子进程」，
      // 干掉引导器会把子进程剩成孤儿，端口和安装目录里的 exe 依旧被占。
      // /T 连整棵进程树一起收掉。
      spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true });
    } else {
      // 负号 = 杀掉整个进程组（spawn 时用了 detached）
      try { process.kill(-pid, 'SIGTERM'); } catch (_) { child.kill('SIGTERM'); }
    }
  } catch (_) {
    /* 进程可能已经没了，忽略 */
  }
}

// ---------------------------------------------------------------------------
// 窗口
// ---------------------------------------------------------------------------
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 960,
    minHeight: 640,
    title: 'chaierww',
    autoHideMenuBar: true,
    backgroundColor: '#0B0B0E',
    icon: IS_WIN ? path.join(__dirname, 'build', 'icon.ico') : undefined,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      // 版本号经启动参数传给 preload。
      // 不能用 require('electron').app.getVersion() —— `app` 只有主进程有，
      // preload 跑在渲染进程里拿到的是 undefined，一调用就抛，整个 contextBridge
      // 注入随之失败，前端会以为自己在浏览器里（表现为：设置里冒出接口文档/Wiki、
      // 「更新」入口消失、检查更新点了没反应）。
      additionalArguments: [`--chaierww-version=${app.getVersion()}`],
    },
  });

  mainWindow.loadURL(APP_URL);
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  mainWindow.on('close', (event) => {
    if (!quitting) {
      // 关闭窗口 = 最小化到托盘，而非退出（保持后端常驻）。
      event.preventDefault();
      mainWindow.hide();
    }
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

// ---------------------------------------------------------------------------
// 托盘
// ---------------------------------------------------------------------------
/**
 * 托盘图标 = logo 本身（跟应用图标、任务栏图标、网页 favicon 同一份源图缩出来的）。
 *
 * 两个平台差异：
 *
 *  - **macOS 用模板图**：`trayTemplate.png` 只有黑字形 + alpha，系统按菜单栏
 *    明暗自动反色，不需要我们挑颜色。
 *  - **Windows / Linux 得自己挑**：logo 是纯黑字形，深色任务栏上就是看不见的
 *    黑影，所以出两套图，按 `nativeTheme.shouldUseDarkColors` 选——浅色任务栏
 *    黑字形（tray-light），深色任务栏白字形（tray-dark）。系统主题变了要换图，
 *    见 createTray 里的 nativeTheme 监听。
 *
 * 文件都在 `assets/` 而不是 `build/`：`build/` 是 electron-builder 的
 * buildResources 目录，会被排除在 app.asar 之外——放那里会出现
 * 「开发时看得见、装完就没了」。缺文件一律回退到下一候选，绝不因为图标缺失崩掉。
 */
function trayIcon() {
  let candidates;
  if (IS_MAC) {
    candidates = ['trayTemplate.png', 'tray-light.png'];
  } else if (nativeTheme.shouldUseDarkColors) {
    candidates = ['tray-dark.ico', 'tray-dark.png', 'tray-light.ico', 'tray-light.png'];
  } else {
    candidates = ['tray-light.ico', 'tray-light.png', 'tray-dark.ico', 'tray-dark.png'];
  }

  for (const name of candidates) {
    const file = path.join(__dirname, 'assets', name);
    if (!fs.existsSync(file)) continue;
    const image = nativeImage.createFromPath(file);
    if (image.isEmpty()) continue;
    if (IS_MAC && name.startsWith('trayTemplate')) image.setTemplateImage(true);
    return image;
  }
  return nativeImage.createEmpty();
}

function createTray() {
  tray = new Tray(trayIcon());
  tray.setToolTip('chaierww');
  refreshTrayMenu();
  // Windows / Linux：左键切换窗口显示；macOS 左键弹出菜单是系统习惯，交给 setContextMenu。
  if (!IS_MAC) {
    tray.on('click', () => {
      if (!mainWindow) return;
      if (mainWindow.isVisible()) mainWindow.hide();
      else mainWindow.show();
    });
  }
  // 系统切深浅色时换图，否则切到深色主题后托盘会变成一块看不见的黑影。
  nativeTheme.on('updated', () => {
    if (tray && !tray.isDestroyed()) tray.setImage(trayIcon());
  });
}

/**
 * 托盘右键菜单。
 *
 * 故意不放「检查更新」：更新入口统一收在「设置 → 常规」里，那里能看到版本号、
 * 检查结果、下载进度和重启按钮。托盘上只留打开窗口和退出两件事。
 */
function refreshTrayMenu() {
  if (!tray) return;
  const menu = Menu.buildFromTemplate([
    { label: '打开 chaierww', click: () => showWindow() },
    { type: 'separator' },
    { label: '退出', click: () => { quitting = true; app.quit(); } },
  ]);
  tray.setContextMenu(menu);
}

function showWindow() {
  if (!mainWindow) {
    createWindow();
    return;
  }
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

// ---------------------------------------------------------------------------
// 更新偏好
// ---------------------------------------------------------------------------
/**
 * 只有一个开关，不值得上 electron-store，直接写一个小 JSON 到 userData。
 *
 * 路径用函数延迟求值：`app.getPath()` 必须先等 app ready，模块加载时就调用会抛。
 * 读失败一律回落到默认值——配置文件坏了不该让应用起不来。
 */
function updatePrefsPath() {
  return path.join(app.getPath('userData'), 'update-prefs.json');
}

const updatePrefs = { autoCheck: true };

function loadUpdatePrefs() {
  try {
    const parsed = JSON.parse(fs.readFileSync(updatePrefsPath(), 'utf8'));
    if (typeof parsed.autoCheck === 'boolean') updatePrefs.autoCheck = parsed.autoCheck;
  } catch {
    /* 首次启动没有这个文件，或内容坏了 —— 保持默认值 */
  }
  return updatePrefs;
}

function saveUpdatePrefs(patch) {
  if (patch && typeof patch.autoCheck === 'boolean') {
    updatePrefs.autoCheck = patch.autoCheck;
  }
  try {
    fs.writeFileSync(updatePrefsPath(), JSON.stringify(updatePrefs, null, 2), 'utf8');
  } catch {
    /* 写不进去也不影响本次会话，下次启动回到旧值 */
  }
  return updatePrefs;
}

// ---------------------------------------------------------------------------
// 自动更新：状态机 + 手动确认 + 进度同步
// ---------------------------------------------------------------------------
/**
 * status 取值：
 *   idle          还没查过
 *   disabled      开发模式 / 没有更新源，检查不可用
 *   checking      正在查
 *   up-to-date    已是最新
 *   available     有新版本，等用户确认下载
 *   downloading   下载中（percent 有值）
 *   downloaded    下载完，等用户重启安装
 *   error         检查或下载失败（message 有值）
 */
const updateState = {
  status: 'idle',
  currentVersion: app.getVersion(),
  version: null,
  releaseNotes: '',
  percent: 0,
  message: '',
  checkedAt: null,
  /** 只有内部用：这一轮检查是不是用户主动点的。 */
  manualCheck: false,
};

function setUpdateState(patch) {
  Object.assign(updateState, patch);
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('update:state', updateState);
  }
}

function releaseNotesText(info) {
  const raw = info && info.releaseNotes;
  if (!raw) return '';
  if (typeof raw === 'string') return raw;
  if (Array.isArray(raw)) {
    return raw.map((item) => `· ${item.version ? `${item.version}：` : ''}${item.note || ''}`).join('\n');
  }
  return '';
}

async function promptDownload(info) {
  const { response } = await dialog.showMessageBox({
    type: 'info',
    title: '发现新版本',
    message: `发现 chaierww ${info.version}`,
    detail: `当前版本 ${app.getVersion()}。是否现在下载更新？下载完成后重启即可生效。`,
    buttons: ['立即下载', '稍后'],
    defaultId: 0,
    cancelId: 1,
  });
  if (response === 0) {
    setUpdateState({ status: 'downloading', percent: 0 });
    autoUpdater.downloadUpdate().catch((err) => {
      setUpdateState({ status: 'error', message: String(err && err.message ? err.message : err) });
    });
  }
}

async function promptInstall() {
  const { response } = await dialog.showMessageBox({
    type: 'info',
    title: '更新已就绪',
    message: `chaierww ${updateState.version} 已下载完成`,
    detail: '重启应用即可完成升级。',
    buttons: ['立即重启', '稍后'],
    defaultId: 0,
    cancelId: 1,
  });
  if (response === 0) installUpdate();
}

function installUpdate() {
  quitting = true;
  // isSilent=false 走安装向导，isForceRunAfter=true 装完自动拉起
  autoUpdater.quitAndInstall(false, true);
}

async function checkForUpdates({ manual = false } = {}) {
  if (!app.isPackaged) {
    setUpdateState({
      status: 'disabled',
      message: '开发模式下不检查更新（未打包的版本没有更新源）。',
    });
    if (manual) {
      dialog.showMessageBox({
        type: 'info',
        title: '检查更新',
        message: '开发模式下不检查更新',
        detail: '只有打包安装后的版本才会连更新源。',
      });
    }
    return updateState;
  }

  if (updateState.status === 'checking' || updateState.status === 'downloading') {
    return updateState;
  }

  // 记下这次是不是用户主动点的：主动查要知道「已是最新」，自动查不该弹框打扰。
  updateState.manualCheck = Boolean(manual);
  setUpdateState({ status: 'checking', message: '', percent: 0 });
  try {
    await autoUpdater.checkForUpdates();
  } catch (err) {
    const message = String(err && err.message ? err.message : err);
    setUpdateState({ status: 'error', message, checkedAt: new Date().toISOString() });
    if (manual) {
      dialog.showMessageBox({
        type: 'error',
        title: '检查更新失败',
        message: '没能连上更新源',
        detail: `${message}\n\n常见原因：网络不通、GitHub 被墙，或最新版本还没发布到 Releases。`,
      });
    }
  }
  return updateState;
}

function setupAutoUpdater() {
  // autoDownload 关闭：先问用户「要不要更新」，而不是闷头下载几百兆。
  autoUpdater.autoDownload = false;
  // 用户选择「稍后」时，退出应用时自动装上，不用再回来点一次。
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on('checking-for-update', () => {
    setUpdateState({ status: 'checking' });
  });

  autoUpdater.on('update-available', (info) => {
    setUpdateState({
      status: 'available',
      version: info.version,
      releaseNotes: releaseNotesText(info),
      checkedAt: new Date().toISOString(),
      message: '',
    });
    void promptDownload(info);
  });

  autoUpdater.on('update-not-available', () => {
    setUpdateState({
      status: 'up-to-date',
      version: null,
      percent: 0,
      checkedAt: new Date().toISOString(),
      message: '',
    });
    if (updateState.manualCheck) {
      dialog.showMessageBox({
        type: 'info',
        title: '检查更新',
        message: '已经是最新版本',
        detail: `当前版本 ${app.getVersion()}。`,
      });
    }
  });

  autoUpdater.on('download-progress', (progress) => {
    setUpdateState({
      status: 'downloading',
      percent: Math.round(progress.percent || 0),
      message: '',
    });
  });

  autoUpdater.on('update-downloaded', (info) => {
    setUpdateState({
      status: 'downloaded',
      version: info.version,
      percent: 100,
      message: '',
    });
    void promptInstall();
  });

  autoUpdater.on('error', (err) => {
    const message = String(err && err.message ? err.message : err);
    setUpdateState({ status: 'error', message, checkedAt: new Date().toISOString() });
    // 自动检查时静默：首次启动没发版、断网都很常见，不该弹一堆框。
    if (updateState.manualCheck) {
      dialog.showMessageBox({
        type: 'error',
        title: '更新出错',
        message: '更新过程中出错',
        detail: message,
      });
    }
  });

  // 启动 8 秒后静默查一次；查不到就算了，查到就弹「发现新版本」。
  // 用户可以在「设置 → 常规」里关掉自动检查，关掉后只有手动点才查。
  if (updatePrefs.autoCheck) {
    setTimeout(() => { void checkForUpdates({ manual: false }); }, 8000);
  }
}

// ---------------------------------------------------------------------------
// 渲染进程 IPC
// ---------------------------------------------------------------------------
ipcMain.handle('update:get', () => updateState);
ipcMain.handle('update:check', () => checkForUpdates({ manual: true }));
ipcMain.handle('update:download', async () => {
  if (updateState.status !== 'available') return updateState;
  setUpdateState({ status: 'downloading', percent: 0 });
  try {
    await autoUpdater.downloadUpdate();
  } catch (err) {
    setUpdateState({ status: 'error', message: String(err && err.message ? err.message : err) });
  }
  return updateState;
});
ipcMain.handle('update:install', () => {
  if (updateState.status === 'downloaded') installUpdate();
  return true;
});
ipcMain.handle('update:get-prefs', () => updatePrefs);
ipcMain.handle('update:set-prefs', (_event, patch) => saveUpdatePrefs(patch || {}));

// ---------------------------------------------------------------------------
// 应用生命周期
// ---------------------------------------------------------------------------
app.whenReady().then(async () => {
  Menu.setApplicationMenu(null); // 隐藏菜单栏（autoHideMenuBar 之外的兜底）

  startBackend();
  const ok = await waitForBackend();
  if (!ok) {
    dialog.showErrorBox(
      'chaierww',
      '后端服务启动超时，请确认环境正常后重试。'
    );
  }

  createWindow();
  createTray();
  loadUpdatePrefs(); // 必须在 setupAutoUpdater 之前：那里要读 autoCheck 决定是否定时检查
  setupAutoUpdater();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
    else showWindow();
  });
});

app.on('before-quit', () => {
  quitting = true;
});

app.on('will-quit', () => {
  stopBackend();
});

// 关窗只是隐藏到托盘，真正退出靠托盘「退出」。所以这里什么都不做，
// 让进程活着（后端也要跟着常驻）。
app.on('window-all-closed', () => {
  /* 故意留空 */
});

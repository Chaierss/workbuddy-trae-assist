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

/**
 * 文档目录（应用内「设置 → Wiki」读它）。
 *
 * 后端 `api/docs.py` 默认按 `__file__` 往上找仓库根，但 PyInstaller onefile 把源码解到
 * 临时目录，安装版那个路径下什么都没有 —— Wiki 会永远是空的。所以打包后显式指到
 * extraResources 打进来的 `resources/docs`；开发模式返回 null，让它继续走仓库目录。
 */
function docsDir() {
  if (!app.isPackaged) return null;
  const candidate = path.join(process.resourcesPath, 'docs');
  return fs.existsSync(candidate) ? candidate : null;
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
  const docs = docsDir();
  if (docs) env.CHAIERWW_DOCS = docs;

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
    // 打包后不指定 icon：`build/` 是 buildResources，不进 asar，这个路径在安装版
    // 根本不存在；Windows 会自动用 exe 内嵌图标（由 afterPack 写入）。
    icon: app.isPackaged || !IS_WIN ? undefined : path.join(__dirname, 'build', 'icon.ico'),
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
  refreshTrayStatus();
  // Windows / Linux：左键切换窗口显示；macOS 左键弹出菜单是系统习惯，交给 setContextMenu。
  if (!IS_MAC) {
    tray.on('click', () => {
      if (!mainWindow) return;
      // 有待处理更新且窗口藏着时，左键的意图基本就是「看看怎么了」。
      if (hasPendingUpdate() && !mainWindow.isVisible()) {
        showWindow();
        sendPrompt(updateState.status);
        return;
      }
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
 * 平时只有「打开」和「退出」两项 —— 更新是一年点不了几次的事，不该常驻占一格。
 * 只有真的有待处理更新（有新版本 / 已下载待重启）时才临时插一行，
 * 点它就唤回窗口并弹更新弹窗。
 */
function refreshTrayMenu() {
  if (!tray) return;
  const items = [{ label: '打开 chaierww', click: () => showWindow() }];
  if (hasPendingUpdate()) {
    const label =
      updateState.status === 'downloaded'
        ? `重启以安装 ${updateState.version}`
        : `查看更新 ${updateState.version}`;
    items.push({
      label,
      click: () => {
        showWindow();
        sendPrompt(updateState.status);
      },
    });
  }
  items.push({ type: 'separator' });
  items.push({ label: '退出', click: () => { quitting = true; app.quit(); } });
  tray.setContextMenu(Menu.buildFromTemplate(items));
}

/**
 * 托盘提示：平时就是应用名，有更新时带上状态。
 *
 * 这是窗口躲在托盘里时**唯一**能让用户察觉「有新版本 / 正在下载」的通道 ——
 * 窗口不可见时任务栏 overlay 图标和 flashFrame 都不存在，系统通知又会被
 * 专注助手吞掉，所以不指望那些。
 */
function refreshTrayStatus() {
  if (!tray || tray.isDestroyed()) return;
  let text = 'chaierww';
  if (updateState.status === 'available' && !isSkipped(updateState.version)) {
    text = `chaierww · 新版 ${updateState.version} 可下载`;
  } else if (updateState.status === 'downloading') {
    text = `chaierww · 正在下载 ${updateState.percent}%`;
  } else if (updateState.status === 'downloaded') {
    text = `chaierww · ${updateState.version} 已下载，重启生效`;
  }
  tray.setToolTip(text);
  refreshTrayMenu();
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
 * 两个开关，不值得上 electron-store，直接写一个小 JSON 到 userData。
 *
 * `skippedVersion` 是「跳过这个版本」记住的版本号：只存字符串，发新版自然失效；
 * 卸载/重装不影响它（在 userData 里，不在安装目录）。null 表示没跳过任何版本。
 *
 * 路径用函数延迟求值：`app.getPath()` 必须先等 app ready，模块加载时就调用会抛。
 * 读失败一律回落到默认值——配置文件坏了不该让应用起不来。
 */
function updatePrefsPath() {
  return path.join(app.getPath('userData'), 'update-prefs.json');
}

const updatePrefs = { autoCheck: true, skippedVersion: null };

function loadUpdatePrefs() {
  try {
    const parsed = JSON.parse(fs.readFileSync(updatePrefsPath(), 'utf8'));
    if (typeof parsed.autoCheck === 'boolean') updatePrefs.autoCheck = parsed.autoCheck;
    if (typeof parsed.skippedVersion === 'string' && parsed.skippedVersion.trim()) {
      updatePrefs.skippedVersion = parsed.skippedVersion.trim();
    }
  } catch {
    /* 首次启动没有这个文件，或内容坏了 —— 保持默认值 */
  }
  return updatePrefs;
}

function saveUpdatePrefs(patch) {
  if (patch && typeof patch.autoCheck === 'boolean') {
    updatePrefs.autoCheck = patch.autoCheck;
  }
  // 显式传 null / 空串 = 取消跳过，所以不能用 truthy 判断。
  if (patch && 'skippedVersion' in patch) {
    const next = patch.skippedVersion;
    updatePrefs.skippedVersion =
      typeof next === 'string' && next.trim() ? next.trim() : null;
  }
  try {
    fs.writeFileSync(updatePrefsPath(), JSON.stringify(updatePrefs, null, 2), 'utf8');
  } catch {
    /* 写不进去也不影响本次会话，下次启动回到旧值 */
  }
  return updatePrefs;
}

/** 这个版本是不是用户主动跳过的。 */
function isSkipped(version) {
  return Boolean(version) && updatePrefs.skippedVersion === version;
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
  /** 已下载字节 / 总字节 / 瞬时速率：给弹窗显示「12.3 MB / 129 MB · 2.1 MB/s」。 */
  transferred: 0,
  total: 0,
  bytesPerSecond: 0,
  /** 归类后的错误原因，渲染层据此挑文案；原始英文 message 只放在「详情」里。 */
  errorKind: '',
  message: '',
  checkedAt: null,
};

/**
 * 这一轮检查是不是用户主动点的。
 *
 * 放在模块级而不是塞进 updateState：这个字段要从主进程推到渲染进程，但渲染端
 * 不需要它，塞进去只会让两边的类型定义对不上。
 */
let manualCheck = false;

/** 8 秒自动检查的定时器。留着句柄才能在退出时清掉，避免 timer 撑着事件循环。 */
let autoCheckTimer = null;

/** 下载是否在进行中，防止弹窗和设置页同时点出两次 downloadUpdate。 */
let downloadInFlight = false;

function setUpdateState(patch) {
  Object.assign(updateState, patch);
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('update:state', updateState);
  }
  refreshTrayStatus();
}

/**
 * 把原始错误归类成人能看懂的一类。
 *
 * 以前的做法是把 `ERR_UPDATER_*` 加完整 URL 直接塞进弹框，用户看到一屏英文
 * 只能得出「坏了」。归类的价值在于能给出下一步：网络问题就换网络，没发布就等，
 * 校验失败就重下。
 */
function classifyError(err) {
  const text = String((err && (err.message || err.code)) || err || '');
  if (/net::|ENOTFOUND|ETIMEDOUT|ECONNREFUSED|ECONNRESET|EAI_AGAIN|getaddrinfo|socket hang up|ERR_INTERNET_DISCONNECTED/i.test(text)) {
    return 'network';
  }
  if (/sha512|integrity|checksum/i.test(text)) return 'checksum';
  if (/ENOSPC|EACCES|EPERM|disk full|no space/i.test(text)) return 'disk';
  if (/404|latest\.ya?ml|Cannot find latest|not found/i.test(text)) return 'noRelease';
  if (/download/i.test(text)) return 'download';
  return 'unknown';
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

/**
 * 有没有「等着用户处理」的更新。
 *
 * 被跳过的版本不算：用户已经明确表示不想看到它，托盘不该继续亮着。
 */
function hasPendingUpdate() {
  if (updateState.status === 'downloaded') return true;
  if (updateState.status === 'available') return !isSkipped(updateState.version);
  return false;
}

/** 通知渲染层「该弹更新弹窗了」。只发事件，不 show / 不 focus。 */
function sendPrompt(kind) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.webContents.send('update:prompt', { kind });
}

/**
 * 「有新版本了」要不要打扰用户。
 *
 * 规则：窗口在屏幕上（且没最小化）才发弹窗事件；在托盘里就只更新 tooltip 和菜单，
 * 等用户自己点开。8 秒后的自动检查可能撞上开会、演示、全屏视频，任何时候
 * 抢焦点都是负收益。
 */
function maybeNotify(kind) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (!mainWindow.isVisible() || mainWindow.isMinimized()) return;
  sendPrompt(kind);
}

/** 开始下载。弹窗和设置页两个入口共用，靠 downloadInFlight 防重入。 */
function startDownload() {
  if (downloadInFlight) return updateState;
  if (updateState.status !== 'available') return updateState;
  downloadInFlight = true;
  setUpdateState({
    status: 'downloading',
    percent: 0,
    transferred: 0,
    total: 0,
    bytesPerSecond: 0,
    errorKind: '',
    message: '',
  });
  autoUpdater
    .downloadUpdate()
    .catch((err) => {
      setUpdateState({
        status: 'error',
        errorKind: classifyError(err),
        message: String(err && err.message ? err.message : err),
        checkedAt: new Date().toISOString(),
      });
    })
    .finally(() => {
      downloadInFlight = false;
    });
  return updateState;
}

function installUpdate() {
  quitting = true;
  // isSilent=false 走安装向导（静默装的话用户看不到进度条，也更难判断卡在哪），
  // isForceRunAfter=true 装完自动拉起后端与窗口。
  autoUpdater.quitAndInstall(false, true);
}

async function checkForUpdates({ manual = false } = {}) {
  manualCheck = Boolean(manual);

  if (!app.isPackaged) {
    setUpdateState({
      status: 'disabled',
      message: '开发模式下不检查更新（未打包的版本没有更新源）。',
    });
    // 手动点的时候要有反馈，否则设置页看起来「点了没反应」。
    if (manual) sendPrompt('unavailable');
    return updateState;
  }

  // downloaded 之后没必要再查：包已经在本地，重查只会把状态打回 checking。
  if (
    updateState.status === 'checking' ||
    updateState.status === 'downloading' ||
    updateState.status === 'downloaded'
  ) {
    return updateState;
  }

  setUpdateState({ status: 'checking', message: '', errorKind: '', percent: 0 });
  try {
    await autoUpdater.checkForUpdates();
  } catch (err) {
    setUpdateState({
      status: 'error',
      errorKind: classifyError(err),
      message: String(err && err.message ? err.message : err),
      checkedAt: new Date().toISOString(),
    });
    if (manual) sendPrompt('error');
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
    const skipped = isSkipped(info.version);
    setUpdateState({
      status: 'available',
      version: info.version,
      releaseNotes: releaseNotesText(info),
      checkedAt: new Date().toISOString(),
      errorKind: '',
      message: '',
    });
    // 跳过的版本：自动检查就当没看见（不弹、不亮徽标、不改 tooltip）。
    // 手动检查照样弹 —— 用户主动问「有没有新版」，静默反而像坏了。
    if (!skipped || manualCheck) sendPrompt('available');
  });

  autoUpdater.on('update-not-available', () => {
    setUpdateState({
      status: 'up-to-date',
      version: null,
      percent: 0,
      errorKind: '',
      checkedAt: new Date().toISOString(),
      message: '',
    });
    // 「已是最新」在设置卡片里已经写着，不再弹框；手动点的人看得见状态变化。
  });

  autoUpdater.on('download-progress', (progress) => {
    setUpdateState({
      status: 'downloading',
      percent: Math.round(progress.percent || 0),
      transferred: progress.transferred || 0,
      total: progress.total || 0,
      bytesPerSecond: progress.bytesPerSecond || 0,
      message: '',
    });
  });

  autoUpdater.on('update-downloaded', (info) => {
    setUpdateState({
      status: 'downloaded',
      version: info.version,
      percent: 100,
      errorKind: '',
      message: '',
    });
    maybeNotify('downloaded');
  });

  autoUpdater.on('error', (err) => {
    const downloading = updateState.status === 'downloading';
    setUpdateState({
      status: 'error',
      errorKind: classifyError(err),
      message: String(err && err.message ? err.message : err),
      checkedAt: new Date().toISOString(),
    });
    // 自动检查时静默：首次启动没发版、断网都很常见，不该弹一堆框。
    // 但「下载到一半失败」必须说 —— 用户以为在下，其实早停了。
    if (manualCheck || downloading) maybeNotify('error');
  });

  // 启动 8 秒后静默查一次。查不到就算了，查到也只在托盘与设置页提示。
  // 用户可以在「设置 → 常规」里关掉自动检查，关掉后只有手动点才查。
  if (updatePrefs.autoCheck) {
    autoCheckTimer = setTimeout(() => { void checkForUpdates({ manual: false }); }, 8000);
  }
}

// ---------------------------------------------------------------------------
// 渲染进程 IPC
// ---------------------------------------------------------------------------
ipcMain.handle('update:get', () => updateState);
ipcMain.handle('update:check', () => checkForUpdates({ manual: true }));
ipcMain.handle('update:download', async () => startDownload());
ipcMain.handle('update:install', () => {
  if (updateState.status === 'downloaded') installUpdate();
  return true;
});
ipcMain.handle('update:get-prefs', () => updatePrefs);
ipcMain.handle('update:set-prefs', (_event, patch) => {
  const next = saveUpdatePrefs(patch || {});
  // 开关状态变了，托盘里那行「查看更新」要跟着消失或出现。
  refreshTrayStatus();
  return next;
});
/**
 * 跳过 / 取消跳过某个版本。
 *
 * 单独开两个通道而不是复用 `update:set-prefs`，是因为「跳过」在语义上是
 * 「对当前这个版本做的决定」，让渲染层自己拼 `{skippedVersion: version}`
 * 容易传成 undefined 而静默失败。
 */
ipcMain.handle('update:skip', (_event, version) => {
  const next = saveUpdatePrefs({ skippedVersion: typeof version === 'string' ? version : null });
  refreshTrayStatus();
  return next;
});
ipcMain.handle('update:unskip', () => {
  const next = saveUpdatePrefs({ skippedVersion: null });
  refreshTrayStatus();
  return next;
});

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
  // 定时器不清掉的话，退出前那 8 秒内计时还在跑，可能在拆后端的同时去查更新。
  if (autoCheckTimer) {
    clearTimeout(autoCheckTimer);
    autoCheckTimer = null;
  }
  stopBackend();
});

// 关窗只是隐藏到托盘，真正退出靠托盘「退出」。所以这里什么都不做，
// 让进程活着（后端也要跟着常驻）。
app.on('window-all-closed', () => {
  /* 故意留空 */
});

# chaierww 桌面端

把 chaierww 的 Web 前端 + FastAPI 后端套成桌面安装包，带应用内更新。

**只有 Windows。** Electron 外壳本身是跨平台的，但后端只实现了 Windows
（DPAPI 加密 + `APPDATA` 目录定位），所以不发 mac/Linux 包。详见[平台](#平台)。

## 架构

```
┌─────────────────────────────────────────────┐
│  Electron 主进程 (main.js)                    │
│  ├─ 拉起内嵌后端（PyInstaller 打包的二进制）    │
│  ├─ 主窗口加载 http://127.0.0.1:57910        │
│  ├─ 单实例锁 / 托盘 / 退出回收后端            │
│  └─ electron-updater 检查更新 → 询问 → 升级   │
├─────────────────────────────────────────────┤
│  chaierww-backend(.exe)                      │
│  ├─ FastAPI 后端（src/chaierww）             │
│  └─ 托管前端产物（CHAIERWW_WEBUI 指向 webui） │
└─────────────────────────────────────────────┘
```

- **后端**：`python scripts/build_backend.py` 用 PyInstaller 打成单文件（Windows 带 `.exe`，其它平台无后缀）
- **前端**：`web/dist` 构建产物，通过 `extraResources` 打进安装包，后端用 `CHAIERWW_WEBUI` 环境变量定位
- **更新**：`electron-updater` 走 GitHub Releases，**先问用户再下载**，详见下节

## 检查更新是怎么走完一圈的

主进程是唯一的真相来源，前端只负责画状态：

```
启动 8s ──► autoCheck 开关开着？ ──否──► 什么都不做
                 │是
                 ▼
           checkForUpdates(自动)
                 │
                 ├─ 无新版本 → 静默（不打扰）
                 └─ 有新版本 → 只更新托盘 tooltip / 侧栏小圆点 / 设置页状态
                                  │
                             用户点开（托盘菜单「查看更新」/ 设置页 / 手动检查）
                                  │
                             自绘弹窗：release notes + [立即下载][稍后][跳过此版本]
                                  │
                          download-progress ──► 弹窗进度 + 速率 + 剩余时间
                                  │
                          update-downloaded ──► 弹窗「立即重启并安装 / 稍后重启」
```

两条入口共用这一个状态机：

| 入口 | 行为 |
| --- | --- |
| 启动自动检查 | 查到也**不弹窗**，只在托盘与设置页提示；查不到 / 出错都静默。可在设置里关掉 |
| 设置 → 常规 → 版本与更新 | 手动：明确告诉你「已是最新」或失败原因，带进度条、速率、剩余时间和「重启并安装」 |

**弹窗是自绘的，不再用系统原生 `dialog.showMessageBox`**：原生框按钮最多两个、塞不进
release notes、也没有进度条。现在由 `web/src/components/UpdateDialog.vue` 渲染，主进程只推
`update:prompt` 事件。托盘有「查看更新 x.y.z」时，右键或左键点一下就能唤回窗口看弹窗。

**「跳过此版本」**记在 `userData/update-prefs.json` 的 `skippedVersion` 里：自动检查碰到它
就当没看见，手动检查仍会提示（弹窗上标「你已跳过」+ 取消跳过）。

状态通过 IPC 推给渲染进程：`preload.js` 暴露 `window.chaierwwDesktop.update`（含
`skip` / `unskip` / `onPrompt`）与 `getPrefs` / `setPrefs`，前端经 `stores/update.ts`
单例分发到 `views/settings/UpdateSection.vue` 与 `components/UpdateDialog.vue`。
状态机字段见 `main.js` 的 `updateState`，自动检查偏好存在 `userData/update-prefs.json`。

> **发布时注意**：`latest.yml` 必须和安装包一起上传到同一个 Release，且**不要改名**——
> electron-updater 按这个文件里的 `path` 取包，名字对不上就是 404。CI
> （`.github/workflows/build-desktop.yml`）会自动带上。

## 平台

| 平台 | 产物 | 更新 |
| --- | --- | --- |
| Windows 10 / 11 x64 | `chaierww-Setup-<版本号>.exe`（NSIS，默认仅当前用户、免管理员） | ✅ 支持 |

**为什么不做 macOS / Linux。** Electron 外壳是跨平台的——托盘、窗口、进程回收、更新
都写了平台分支，安装包也能打出来。但后端只有 Windows 实现：`infra/secrets.py` 的凭据
加密是 DPAPI，两个 adapter 的 `default_paths()` 依赖 `APPDATA` / `LOCALAPPDATA`。
所以 mac/Linux 装完启动时后端抛 `PlatformNotSupported`，外壳弹「后端服务已意外退出」。

发一个下了用不了的包没有意义，CI 矩阵只留了 `windows-latest`。要补的话按这个顺序：

1. `secrets.py` 换跨平台加密（macOS Keychain / Linux Secret Service 或 libsodium）
2. `adapters/` 按平台解析目录
3. `CLIENT_EXE_NAMES` 去掉 `.exe` 硬编码
4. `domain/wb_tasks.py` 里 `os` / `arch` 的上报值改真实值
5. CI 矩阵加回 `macos-latest` / `ubuntu-latest`，其余构建步骤不用改

`package.json` 里的 `dist:mac` / `dist:linux` 脚本保留着，配合上面的工作用。
`main.js` 里已有的跨平台分支（mac 关窗不退、Linux 托盘依赖 `libappindicator`、
`kill(-pid)` 收进程组等）也先留着，不是死代码。

> 产物名**带版本号**（`chaierww-Setup-0.1.6.exe`）：手动下载时从文件名就能分清新旧，
> 浏览器缓存 / 镜像也不会拿旧文件糊你。`latest.yml` 里的 `path` 自动跟着产物名走
> （取的是产物 basename，不是 `artifactName` 模板），electron-updater 照常工作，
> 不用额外维护。

**PyInstaller 不能交叉编译。** 打哪个平台的包就要在哪个平台上跑，所以构建放在
`.github/workflows/build-desktop.yml` 里；推 `v*` 标签自动构建并发版。

## 安装包向导

NSIS 安装器（`oneClick: false`，辅助向导）比「一键装」多了几步，但换来的是能选路径、
能看清装到哪。相关资源都在 `build/` 下（buildResources）：

| 资源 | 用途 |
| --- | --- |
| `build/license.txt` | 安装协议页（`MUI_PAGE_LICENSE`）。**UTF-8 带 BOM**，约 15 行中文要点 + 指向安装目录里的全文 |
| `build/installerHeader.bmp` | 150×57，内页页眉右侧的品牌图 |
| `build/installerSidebar.bmp` | 164×314，欢迎页/完成页左侧品牌图（卸载侧栏缺省复用） |
| `build/INSTALL.txt` | 打进安装目录的说明：常驻托盘、数据位置、怎么卸载（完成页的「打开使用说明」链接也指向它） |

三份文件随包分发到安装目录：`LICENSE.txt`（PolyForm 全文）、`DISCLAIMER.md`（免责声明）、
`INSTALL.txt`（`extraFiles` 配置）。协议页只放要点，完整法律文本以安装目录里的为准。

向导一共五页：**欢迎 → 协议 → 安装目录 → 安装中 → 完成**。

界面语言在 `package.json` 的 `build.nsis` 里定：`language: "2052"` +
`installerLanguages: ["zh_CN"]`。不设的话 electron-builder 默认 `1033`（英文），
正文是中文、按钮却是 `Next / Install / Finish`；只设 `language` 也不行——默认会把
NSIS 自带的全套语言编进去再按系统区域挑，英文系统上又会回退成英文界面。

`build/installer.nsh` 里跟界面有关的动作：

- **欢迎页**（`customWelcomePage` → `MUI_PAGE_WELCOME`）：electron-builder 的辅助向导
  **默认不插欢迎页**，所以 `MUI_WELCOMEPAGE_*` 那组 define 不先补页就是死配置，
  用户打开安装包第一屏直接是协议要点。这里只多插一个**标准**页，不接管向导流程。
- **跳过「为所有用户 / 仅为我安装」**（`customInstallMode`）：模板里的安装范围页
  （`multiUserUi.nsh`）对本软件没用——只写 HKCU、装当前用户目录，留着只会多一次 UAC 提权。
  置 `$isForceCurrentInstall=1` 后该页在 PRE 回调里 `Abort`，等于跳过。
- **文案**（`MUI_WELCOMEPAGE_*` / `MUI_DIRECTORYPAGE_*` / `MUI_FINISHPAGE_*`）：这些是
  MUI2 的「默认值」——在 `!include MUI2.nsh` 之前 `!define`，MUI_DEFAULT 就不会覆盖。
  **不要**改写成 `customFinishPage` / `customWelcomePage` 里自绘页面：那会整段替换模板、
  连带丢掉 `StartApp` 函数，升级 electron-builder 必炸（`customWelcomePage` 只插标准页，安全）。
  完成页文本区默认只有 40u（约 3 行），长文案要加 `MUI_FINISHPAGE_TEXT_LARGE` 才不被裁。
- **卸载前提示**（`customUnInit` 开头）：弹一次「数据在 `~/.chaierww` 不会被删，重装会回来」，
  允许取消。**用 `IfSilent` 而不是 LogicLib 的 `${IfNot}`**——本文件被前插在
  `!include MUI2.nsh` 之前，LogicLib 还没进来；静默卸载（自动更新带 `/S`）绝不能弹框，
  否则升级会永久卡住。


## 开发模式运行

```bash
cd desktop
npm install
npm start
```

开发模式下不打包后端，而是用 `python -m chaierww.main` 直接跑源码（用 `CHAIERWW_PYTHON` 指定解释器），前端指向 `../web/dist`。
开发模式下「检查更新」会直接返回「当前环境不检查更新」——没打包的版本没有更新源。

## 打包安装包

以下命令都在 `desktop/` 目录下执行（除标注的那一步）：

```bash
# 1. 生成图标（改过 logo 才需要重跑）
python scripts/build_icons.py

# 2. 构建前端
cd ../web && npm run build && cd ../desktop

# 3. 打包后端二进制
python scripts/build_backend.py

# 4. 打包安装包
npm run dist:win
```

产物在 `desktop/release/` 下。

`package.json` 里 `dist:mac` / `dist:linux` 的脚本还在（配合将来补 mac/Linux 时用），
但现在打出来的包启动不了，见上面的[平台](#平台)一节。

### 国内网络 / 非管理员账户的两个坑（Windows）

**坑一：二进制包从 GitHub 下载超时。** electron-builder 会下载 electron、nsis、winCodeSign
等二进制包，默认走 GitHub。先设镜像：

```bash
set ELECTRON_BUILDER_BINARIES_MIRROR=https://cdn.npmmirror.com/binaries/electron-builder-binaries/
```

**坑二：`winCodeSign-2.6.0.7z` 解压失败。** 这个包里含 macOS 符号链接，Windows 非管理员
账户解压必报 `Cannot create symbolic link : 客户端没有所需的特权`，打包卡在 rcedit 环节。
本项目因此关闭了官方 rcedit 步骤：

```json
"win": { "signAndEditExecutable": false }
```

改由 `scripts/afterPack.js` 调用 `build/tools/rcedit-x64.exe` 写入图标与版本信息。
该 rcedit 来自官方 winCodeSign-2.6.0 包，未做修改。

> 副作用：安装包**没有代码签名**。Windows 首次运行会提示「未知发布者」，
> 需要点「更多信息 → 仍要运行」。要消除得买证书。

## 自动更新发布

1. 改 `desktop/package.json` 的 `version`
2. 打标签（`git tag v0.6.1 && git push origin v0.6.1`）→ CI 构建并上传 Release
3. 旧版客户端启动后自动检查 → 弹窗询问 → 下载 → 重启升级

发布通道配置在 `desktop/package.json` 的 `build.publish`（GitHub: `Chaierss/workbuddy-trae-assist`）。
这个仓库名同时决定了安装包里 `app-update.yml` 指向哪里——写错不会报错，只会静默地永远查不到更新。

## 重装时提示「chaierww 无法关闭」

这句提示是 NSIS 的 `appCannotBeClosed`，出现在两个地方：杀不掉旧进程，或者解压新包时
文件被占用。踩过的真实原因是第二种：

1. 安装程序先 `taskkill /im chaierww.exe` 关旧版。主进程被 `/f` 强杀时不走 `will-quit`，
   清理逻辑没机会跑。
2. 后端是 PyInstaller `--onefile`（引导器 + 子进程两层），主进程一死，子进程成了孤儿，
   继续占着 `resources/backend/chaierww-backend.exe` 和 57910 端口。
3. 安装程序要覆盖这个 exe，7z 解压失败 → 报「无法关闭」；而它重试时只查 `chaierww.exe`
   （早没了），所以重试多少次都过不去。

现在的两道保险：

- **主进程退出**：`stopBackend()` 在 Windows 上用 `taskkill /PID <pid> /T /F` 收整棵进程树
  （光 kill 父进程没用）；Linux/macOS 用 `detached` 起的进程组，`kill(-pid)` 整组收。
- **主进程被强杀**：后端自己盯着父进程。主进程通过 `CHAIERWW_PARENT_PID` 传 PID，
  后端 `infra/parent_watch.py` 每秒探一次，父进程没了就 `os._exit(0)`，
  文件锁和端口在 1 秒内放开。（Windows 上不能用 `os.kill(pid, 0)` 探测——CPython 会把它
  翻译成 TerminateProcess，那是「探测即击杀」。）

如果装的是**没有这两道保险的旧版本**，升级前先在任务管理器里手动结束
`chaierww.exe` 和 `chaierww-backend.exe` 两个进程，之后就不会再遇到。

## 打包资源放在哪

图标由 `desktop/scripts/build_icons.py` 从 `web/public/android-chrome-512x512.png` 生成，
但**应用图标和托盘图标放在不同目录**，这不是随手放的：

| 目录 | 内容 | 为什么 |
| --- | --- | --- |
| `build/` | `icon.ico` / `icon.icns` / `icon.png` / `icons/` | electron-builder 的 buildResources，只在打包时被工具读取 |
| `assets/` | `tray-light.*` / `tray-dark.*` / `trayTemplate.*` | 运行时读，**必须**在 buildResources 之外 |

`build/` 会被排除在 `app.asar` 之外，托盘图放进去就会出现「开发时看得见、装完托盘空白」——
踩过一次，装完的版本托盘是一块空白。

托盘图就是 logo 缩出来的，没有另画简化标记：16px 下必然糊，但形状一致才是用户要的。
黑字形在深色任务栏上看不见，所以按系统深浅色出两套图动态切换（`main.js` 的 `trayIcon()`）。

## 运行时结构

- **全量打包**：凭据加密、切换引擎、客户端适配都在 `src/chaierww/` 里，PyInstaller 直接打进
  `chaierww-backend`。安装包与仓库源码一致，不存在需要另外获取的私有包。
- **webui 走 `extraResources`**：后端是独立的 PyInstaller 进程，读不了 Electron 的
  `app.asar` 包内文件，所以前端产物必须落在 asar 之外（`process.resourcesPath/webui`），
  由 `CHAIERWW_WEBUI` 环境变量指给后端。

## 已知取舍

**安装版会带一个控制台窗口。** `scripts/build_backend.py` 里用的是 `--console`，
保留 stdout 便于出问题时看后端日志。改成 `--windowed` 就没有黑窗了，
代价是后端崩了只能靠 `~/.chaierww/logs/` 里的文件排查。当前选择留着控制台。

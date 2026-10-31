# Linux 部署与使用

chaierww 在 Linux 上有两条路：**装桌面包**，或**源码跑后端用浏览器打开**。
先看下面这张表选一条，再看对应的章节。

| | 路径 A：桌面安装包 | 路径 B：源码跑后端 |
| :-- | :-- | :-- |
| 适用 | Ubuntu 20.04+ / Debian 11+ / CentOS 8+ / 主流国产发行版 | 任意 64 位 Linux，含 **CentOS 7** |
| 拿到什么 | 窗口 + 托盘，双击即用 | 浏览器页面，无托盘、无自动更新 |
| 需要会什么 | 解压 / 装包 | 会跑两条命令（脚本已替你拼好） |
| 代价 | 老系统（glibc < 2.31）起不来 | 要自己准备 Python 3.11 和前端产物 |

> 非官方第三方工具，与 WorkBuddy / Trae 及权利人无隶属关系。使用前请读
> [免责声明](policies/DISCLAIMER.md)。

---

## 路径 A：桌面安装包

### A1. 安装

到 [Releases](https://github.com/Chaierss/workbuddy-trae-assist/releases) 下载最新的
`chaierww-x64.AppImage` 或 `chaierww-x64.deb`。

```bash
# AppImage（免安装，单文件）
chmod +x chaierww-x64.AppImage
./chaierww-x64.AppImage

# 没有 FUSE（容器、精简系统、CentOS 常见）：解包运行，不依赖 FUSE
APPIMAGE_EXTRACT_AND_RUN=1 ./chaierww-x64.AppImage

# deb（Debian / Ubuntu）
sudo apt install ./chaierww-x64.deb
```

**CentOS / RHEL / openEuler 不要用 deb**，那是 Debian 系的格式；用 AppImage。

### A2. 运行时依赖

Electron 不是静态链接的，最小化系统常缺库。Ubuntu / Debian：

```bash
sudo apt install libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
  libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
  libasound2 xdg-utils libappindicator3-1
```

CentOS 8+ / RHEL 系（装完再跑，缺什么按报错补）：

```bash
sudo dnf install nss atk at-spi2-atk cups-libs libdrm libxkbcommon \
  libXcomposite libXdamage libXfixes libXrandr mesa-libgbm alsa-lib xdg-utils \
  libappindicator-gtk3
```

- **托盘要 `libappindicator`**（Ubuntu 的包名是 `libappindicator3-1`）。缺了程序照常能开，
  只是没有托盘图标，关窗口后得从任务栏 / Dock 唤回。
- Ubuntu 24.04 起 `libasound2` 改名为 `libasound2t64`，`apt` 会告诉你，照它建议装即可。

### A3. 升级

- **AppImage**：能在应用内更新（设置 → 常规 → 检查更新），前提是它确实以 AppImage
  方式运行（进程里有 `APPIMAGE` 环境变量）。用 `APPIMAGE_EXTRACT_AND_RUN=1` 解包运行的，
  如果检查更新提示不可用，就直接到 Releases 下载新版替换文件。
- **deb**：不能应用内更新，点检查更新会明确告诉你这一点。到 Releases 下载新 deb 覆盖装：

```bash
sudo apt install ./chaierww-x64.deb
```

数据在 `~/.chaierww`，覆盖安装不碰它。

---

## 路径 B：源码跑后端（CentOS 7 唯一可行路径）

浏览器访问 `http://127.0.0.1:57910/`。后端只监听回环，远程访问见[第 5 节](#5-远程访问服务器场景)。

### B1. 一键安装

```bash
git clone https://github.com/Chaierss/workbuddy-trae-assist.git
cd workbuddy-trae-assist
bash scripts/install-linux.sh
```

脚本会依次检查：Python 版本（< 3.11 直接停下并给升级指引）、前端产物 `web/dist`
（缺了就按有没有 Node 判断是本地构建还是提示你去别的机器构建）、然后建 `.venv`、
`pip install -e .`。它**不会** `sudo`、不改系统包。

常用参数：

| 参数 | 作用 |
| :-- | :-- |
| `--python /path/to/python3.11` | 指定解释器（系统默认版本不够时用） |
| `--skip-web` | 明确表示「前端产物我自己处理」，缺 `web/dist` 时不再尝试构建 |
| `--link` | 在 `~/.local/bin/chaierww` 建软链，之后直接敲 `chaierww` |

启动：

```bash
.venv/bin/chaierww
```

（装了 `--link` 就是 `chaierww`，需要 `~/.local/bin` 在 `PATH` 里。）

国内网络慢的话，先设镜像再跑脚本，`pip` 会自动用它：

```bash
export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
```

### B2. 手动等价步骤

不想用脚本，或者想自己控制每一步：

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e .
cd web && npm ci && npm run build && cd ..
.venv/bin/chaierww
```

`web/dist` 必须存在，否则后端只提供 API、页面是空的（启动日志里会有一行
`未找到前端产物（web/dist），当前只提供 API`）。

### B3. 前端产物怎么来

前端要 **Node ≥ 20.19**（vite 7 的要求）。老系统上没有这个版本的 Node，
**在任意一台装了 Node 20/22 的机器上构建好，把产物拷过去**就行，目标机器不需要 Node：

```bash
# 在有 Node 的机器上
git clone https://github.com/Chaierss/workbuddy-trae-assist.git
cd workbuddy-trae-assist/web && npm ci && npm run build

# 拷到目标机器（约几 MB）
rsync -a web/dist user@目标机器:~/workbuddy-trae-assist/web/
```

也可以直接用目标机器上的 `CHAIERWW_WEBUI=/绝对路径/dist` 指过去。

---

## 3. CentOS 7

**先说结论**：CentOS 7 的 glibc 是 2.17，桌面包（Electron 43 + Python 二进制）链接的是
glibc 2.31+，启动直接报 `GLIBC_2.28 not found`。**只能用路径 B**。

三个现实问题与处理：

**① 官方 yum 源已失效**（CentOS 7 于 2024-06 停止维护）。换 vault 源，或用内网镜像：

```bash
sudo sed -i 's|mirrorlist=|#mirrorlist=|g' /etc/yum.repos.d/CentOS-*.repo
sudo sed -i 's|#baseurl=http://mirror.centos.org|baseurl=http://vault.centos.org|g' \
  /etc/yum.repos.d/CentOS-*.repo
```

**② Python 3.11**。系统自带是 3.6/3.8，都不够。两条路：

- **预编译包（推荐）**：从 [python-build-standalone](https://github.com/astral-sh/python-build-standalone/releases)
  下载 `cpython-3.11.*-x86_64-unknown-linux-gnu-install_only.tar.gz`，解压到 `~/.local/python311`，
  然后用 `bash scripts/install-linux.sh --python ~/.local/python311/bin/python3`。
  这类构建面向老 glibc，不需要编译（下载页会标注对应平台，以它为准）。
- **源码编译**：需要 `gcc`、`make`、`openssl-devel`、`zlib-devel`、`libffi-devel`，
  编译一次不短。装依赖本身又要 yum，所以得先解决第 ① 条。

**③ 前端不要在这台机器上构建**：Node 20 的官方二进制要求 glibc ≥ 2.28，CentOS 7 装不上。
按 [B3](#b3-前端产物怎么来) 在别的机器构建后拷贝。

**不要试图在 CentOS 7 上跑 `npm start`（Electron 开发模式）**：同样是 glibc 问题。
`desktop/README.md` 里有同样的说明，这里不重复。

---

## 4. 首次使用，以及它在这台机器上能做什么

1. 打开 `http://127.0.0.1:57910/` → 号池 → 扫码加号（WorkBuddy / Trae）。
2. 点「切换」先看预演，确认后才会真正写入。
3. 之后按需用活动、用量、备份、计划任务。

**能做什么，取决于这台机器装没装客户端：**

| 这台机器上有客户端登录态 | 可以做的事 |
| :-- | :-- |
| 有（装了 WorkBuddy / Trae 并登录过） | 全部：导入、切号、签到、会话同步 |
| 没有（纯服务器） | 号池、扫码加号、用量、活动接口、额度网关；**不能切号**（没有状态文件可写）、会话同步为空 |

切换还要求**客户端已退出**：工具不会边跑边改。检测到进程在跑时会拒绝写入并提示，
这是保护，不是故障。

---

## 5. 远程访问（服务器场景）

后端**固定监听 `127.0.0.1:57910`**，而且守卫会拒绝非回环的 `Host` / `Origin`
（防 DNS 重绑定与跨站请求）。所以不要想「改监听地址从别的机器直接访问」——那条路
被安全模型堵死了，硬改也只会 403。

正确姿势是 SSH 端口转发，在你自己的电脑上：

```bash
ssh -L 57910:127.0.0.1:57910 user@服务器
```

保持这个连接，然后用本机浏览器打开 `http://127.0.0.1:57910/`。此时浏览器看到的
就是回环地址，守卫照常放行。

外部脚本要调用接口，用服务器上的 `~/.chaierww/api-token` 作为 Bearer 令牌，
同样通过隧道访问。

---

## 6. 数据、路径与覆盖变量

所有数据在 `~/.chaierww`：SQLite 库、加密凭据、备份、脱敏日志。拷贝这个目录就是完整迁移。

| 变量 | 作用 | 默认 |
| :-- | :-- | :-- |
| `CHAIERWW_HOME` | 数据目录 | `~/.chaierww` |
| `CHAIERWW_WEBUI` | 前端产物目录 | 源码运行时自动找仓库 `web/dist` |
| `CHAIERWW_TRAE_STATE_DIR` | Trae 登录态目录 | `~/.config/Trae CN/User/globalStorage`（找不到试 `~/.config/Trae`） |
| `CHAIERWW_WORKBUDDY_STATE_DIR` | WorkBuddy 登录态目录 | `~/.config/CodeBuddyExtension/Data/Public/auth` |
| `CHAIERWW_TRAE_APP` / `CHAIERWW_WORKBUDDY_APP` | 客户端主程序路径 | 自动在 `/usr/bin`、`/opt` 下找 |
| `CHAIERWW_API_GUARD=0` | **关闭**接口守卫 | 开。只在完全隔离的环境里用，关掉等于本机任何程序都能调接口 |

路径猜不中时（比如客户端装在自定义目录），优先用上面四个变量显式指定，
别去改代码。

---

## 7. 凭据加密在 Linux 上的边界

Linux 用 AES-256-GCM，密钥由 `/etc/machine-id` + 当前 uid 派生，**不落盘**。

- 挡得住：密文被拷到另一台机器、或同一台机器的另一个用户下——派生不出同一把密钥。
- **挡不住本机 root**：机器标识、库文件、进程内存都在 root 手里。
- `/etc/machine-id` 被重置（重装系统、克隆镜像后重新生成）会让旧密文全部解不开，
  表现为「凭据无法解密」，重新导入账号即可。

完整威胁模型见 [SECURITY.md](../SECURITY.md)。

---

## 8. 排障

| 现象 | 原因 | 处理 |
| :-- | :-- | :-- |
| `GLIBC_2.28 not found` / 双击无反应 | 系统 glibc 太老（CentOS 7 等） | 走路径 B；不要在老系统上装桌面包 |
| 提示 `dlopen: libfuse.so.2` / FUSE 缺失 | AppImage 需要 FUSE | `APPIMAGE_EXTRACT_AND_RUN=1 ./chaierww-x64.AppImage` |
| 托盘没有图标 | 缺 `libappindicator` | 装 `libappindicator3-1`（Debian 系）/ `libappindicator-gtk3`（RHEL 系） |
| 页面空白，只有 API | 缺 `web/dist` | 见 [B3](#b3-前端产物怎么来)；启动日志会有明确 warning |
| 打开就 403「仅允许本机访问」 | 从别的机器直连，或改了监听地址 | 用 [SSH 隧道](#5-远程访问服务器场景) |
| 端口 57910 被占用 | 已有实例在跑，或别的程序占了 | `ss -lp | grep 57910` 看是谁；杀掉旧实例或换机 |
| 「凭据无法解密」 | `/etc/machine-id` 变了，或换了用户 | 重新导入账号 |
| 切换按钮不可用 / 提示客户端在运行 | 客户端没退出 | 退出客户端后重试 |
| 切换后客户端没起来 | 客户端路径猜错 | 用 `CHAIERWW_TRAE_APP` / `CHAIERWW_WORKBUDDY_APP` 指定 |

日志在 `~/.chaierww/logs/`。

---

## 9. 卸载

- **AppImage**：删文件即可；数据目录 `rm -rf ~/.chaierww`（可选，删了就没了）。
- **deb**：`sudo apt remove chaierww`，数据目录同样单独删。
- **源码**：删仓库目录；`--link` 建过的软链一并删掉。

---

英文快速上手：[QUICKSTART.en.md](QUICKSTART.en.md) · 桌面端打包与排障：
[desktop/README.md](../desktop/README.md) · 更新日志：[CHANGELOG.md](../CHANGELOG.md)

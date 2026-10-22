<h1 align="center">chaierww</h1>

<p align="center">
  <b>换一次号，从 60 秒到 1 秒。</b><br>
  <sub>WorkBuddy / Trae 多账号本地切换工作台</sub>
</p>

<p align="center">
  <img alt="平台" src="https://img.shields.io/badge/%E5%B9%B3%E5%8F%B0-Windows%2010%20%2F%2011-2ea043" />
  <img alt="许可" src="https://img.shields.io/badge/%E8%AE%B8%E5%8F%AF-PolyForm%20Noncommercial-4c8bf5" />
  <img alt="无云端" src="https://img.shields.io/badge/%E6%95%B0%E6%8D%AE-%E5%8F%AA%E5%9C%A8%E6%9C%AC%E6%9C%BA-d4a66b" />
  <a href="https://github.com/Chaierss/workbuddy-trae-assist/releases"><img alt="版本" src="https://img.shields.io/github/v/release/Chaierss/workbuddy-trae-assist?color=8a63d2&label=%E7%89%88%E6%9C%AC" /></a>
</p>

<p align="center">
  <a href="#装"><b>下载安装</b></a> ·
  <a href="#它长这样">界面</a> ·
  <a href="#和手动切号比">和手动比</a> ·
  <a href="#常见问题">常见问题</a> ·
  <a href="#开发">开发</a>
</p>

<p align="center">
  <img src="docs/images/overview.png" alt="chaierww 概览界面" width="920">
</p>

## 换一个号，你要花多久

|  | 你要做的事 | 时间 |
|:--|:--|:--|
| **手动** | 退出 · 扫码 · 等同步 · 找回会话 | 约 60 秒 |
| **chaierww** | 点一下 | **1 秒** |

一天换 5 次号，一年下来有 **30 小时** 花在登出重登上。

chaierww 把 WorkBuddy 国内版和 Trae 国内版的账号收进一处，扫码登录一次，
之后换号是一次点击。切换前强制备份，中途失败自动回滚——不会把登录态写坏。

**所有数据只放在你自己的机器上**：没有云端中转，没有账号托管，断网也能切号。

> 非官方第三方工具，与 WorkBuddy / Trae 及其权利人无隶属、授权或背书关系。
> 它会读写本机客户端的认证数据、按你的配置自动发起请求。使用前请读
> **[免责声明](docs/policies/DISCLAIMER.md)**。

## 装

Windows 10 / 11，不用敲任何命令：

1. 到 [Releases](https://github.com/Chaierss/workbuddy-trae-assist/releases) 下 `chaierww-Setup.exe`
2. 双击安装——免管理员，装进当前用户目录
3. 打开后扫码加号，之后点一下就能切过去

> 安装包没有代码签名，Windows 首次运行会拦一次：点「更多信息 → 仍要运行」。
> 这是没有付费证书的成本，不是包有问题。

**升级不用管**：启动 8 秒后静默查一次更新源，发现新版本会弹窗问你要不要下载；
下载完不打断你，下次退出应用时自动装上。也可以在托盘菜单或设置页手动点「检查更新」。

### 平台支持

| 平台 | 状态 |
| :-- | :-- |
| **Windows 10 / 11 (x64)** | 完整功能，有安装包 |
| macOS 11+ / Linux x64 | 安装包能构建出来，但**启动会失败**——见下 |

macOS / Linux 上「客户端登录态读写」和「凭据加密」这两层目前只有 Windows 实现
（后者是 DPAPI），所以后台进程一起来就会报「后端服务已意外退出」。
Electron 外壳与三平台构建矩阵已经就绪，缺的是
`infra/secrets.py` 的跨平台加密实现，以及 `adapters/` 里的目录定位。
**这两块欢迎 PR**，切入点在文末的[开发](#开发)一节。

## 它长这样

| 号池 · 账号、积分、凭据状态一屏看全 | 用量 · 请求 + 积分，按模型、按日趋势 |
| --- | --- |
| ![号池](docs/images/accounts.png) | ![用量](docs/images/usage.png) |

| 活动 · 签到与成长任务逐账号检测 | 网关 · 额度以 OpenAI / Anthropic 兼容接口开放 |
| --- | --- |
| ![活动](docs/images/activity.png) | ![网关](docs/images/gateway-overview.png) |

| 网关 Key · 签发、限模型、随时吊销 | 模型目录 · 可用模型与能力标注 |
| --- | --- |
| ![API Key](docs/images/gateway-keys.png) | ![模型目录](docs/images/gateway-models.png) |

| 计划任务 · 每档只留最近 7 次执行记录 | 备份 · 每日自动一份，按份数滚动保留 7 份 |
| --- | --- |
| ![计划任务](docs/images/settings-schedule.png) | ![备份](docs/images/settings-backup.png) |

| 环境 · 本机路径、客户端状态与能力边界 | 常规 · 换号时迁移哪几类数据，逐项可控 |
| --- | --- |
| ![环境](docs/images/settings-env.png) | ![常规](docs/images/settings-general.png) |

> 截图是脚本渲染完 DOM 再截的：账号名、账号 ID、本机路径、邮箱、网关 Key、IP
> 在快门按下之前就已经在页面里被替换成圆点或模糊，不是事后按坐标手动画的。
> 所以界面一改版也不用重新量坐标。生成方式是 `python scripts/shots.py`。

## 除了切号

- **账号状态一屏看全**：凭据是否可用、积分还剩多少、几天后到期
- **签到和成长任务**逐账号检测，能自动的一键跑完，手动的写明为什么
- **用量**按模型、按日归集，谁在烧额度一眼看出
- **额度能借出去**：以 OpenAI / Anthropic 兼容接口开放给本机其它 AI 工具，Key 可签发可吊销
- **切换可预演**：先告诉你这次会迁移哪些数据、改动哪些文件，零写入
- **操作有台账**：计划任务只留最近 7 次执行记录，成功失败都带原因，不用翻日志文件

## 和手动切号比

| | chaierww | 手动退出重登 |
| :-- | :-- | :-- |
| **切号动作** | 一次点击 | 退出 → 重新登录 → 扫码 → 等验证 |
| **失败处理** | 自动回滚到切换前 | 状态写坏了自己排查 |
| **动手前** | 只读预演，先看会改哪些文件 | 直接改，改坏了才知道 |
| **多号轮换** | 按状态挑号，额度不足直接换 | 手动记账 |
| **数据** | 本机 `~/.chaierww`，每日自动备份 | 本机，无备份 |

## 数据只在你机器上

- 凭据用 Windows DPAPI 按当前用户加密，密钥不落文件；日志里一律脱敏
- 所有数据在 `~/.chaierww`（可用 `CHAIERWW_HOME` 覆盖），整个目录拷走就是完整迁移
- 没有服务端。断网、拔网线，切号照样能用

细节和威胁模型见 **[SECURITY.md](SECURITY.md)**。

## 它不做什么

写在这里是为了划边界，不是免责声明：

| 不做 | 原因 |
|:--|:--|
| 云端中转、账号托管 | 会让工具变成服务 |
| 账号分享、租借、拼车 | 同上 |
| 代登录（收集你的密码） | 凭据责任过重，且没必要 |
| 修改客户端程序本体 | 只读写状态文件，不打补丁 |
| 上传任何数据到服务器 | 数据只落本机 |
| 绕过平台额度或风控 | 它帮你管自己的号，不帮你薅 |

## 常见问题

**切号后客户端没起来 / 秒退？**
先看「概览 → 客户端」里的安装状态。切号走的是与双击图标等价的系统调用，
如果仍失败，多半是路径不对或客户端正在更新——到「设置 → 环境」核对登录目录。

**备份会占很多磁盘吗？**
常驻备份固定 7 份，单份约 30 MB，稳态 200 MB 上下。切换前的临时备份在成功后立即删除。

**会封号吗？**
只在本机读写客户端已有的登录态，不伪造请求、不并发登录、不改动客户端程序。
但任何第三方工具都有风险，**风险由使用者自行承担**。

**支持 macOS / Linux 吗？**
暂不支持，原因见上面的[平台支持](#平台支持)。外壳和打包已经跨平台，缺的是
凭据加密与客户端目录定位这两层。

**升级会丢数据吗？**
不会。`~/.chaierww` 和安装目录是分开的，覆盖安装不碰数据；每日自动备份也照常在跑。

## 开发

仓库是完整的：凭据加密、切换引擎、两个客户端适配器都在 `src/chaierww/` 里，
clone 下来直接能跑，没有需要另外申请的私有包。

```bash
git clone https://github.com/Chaierss/workbuddy-trae-assist.git
cd workbuddy-trae-assist
python -m venv .venv
.venv/Scripts/activate                # macOS / Linux: source .venv/bin/activate
pip install -e ".[dev]"

python -m chaierww.main               # http://127.0.0.1:57910/
pytest                                # 440 个用例
ruff check src tests
```

桌面安装包：前端在 `web/`（`npm ci && npm run build`），Electron 壳在 `desktop/`，
构建方式见 [desktop/README.md](desktop/README.md)。

### 项目结构

```
src/chaierww/
  adapters/    读客户端登录态：workbuddy.py / trae.py，slot 抽象在 base.py
  domain/      切换引擎、活动签到、积分、额度网关、计划任务、自动轮换
  infra/       凭据加密（secrets.py）、SQLite、父进程守护
  api/         本地 HTTP 接口，桌面端与前端共用
web/           Vue 3 + TypeScript + Tailwind 前端
desktop/       Electron 壳、托盘、自动更新、PyInstaller 打包
scripts/       构建、截图脱敏、诊断脚本
tests/         pytest 用例（440 个）
```

想接一个新平台，看 `adapters/base.py` 的 slot 设计——新增一个 adapter 就够，
不用改切换引擎。想补 macOS / Linux 支持，从 `infra/secrets.py` 和两个 adapter 的
`default_paths()` 入手。

### 文档导航

| 文档 | 讲什么 |
| :-- | :-- |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 开发约定、提交规范、测试方式 |
| [SECURITY.md](SECURITY.md) | 安全模型、漏洞报告渠道 |
| [CHANGELOG.md](CHANGELOG.md) | 每个版本改了什么 |
| [desktop/README.md](desktop/README.md) | 打包、自动更新、三平台构建 |
| [docs/policies/DISCLAIMER.md](docs/policies/DISCLAIMER.md) | 免责声明（使用前请读） |

## 许可

[PolyForm Noncommercial License 1.0.0](LICENSE)。

**源码全部公开，但这不是一个 OSI 认可的开源许可。** 准确说法是 source-available：

- ✅ 个人使用、学习、修改、二次分发（非商业目的）
- ❌ 任何商业用途——包括公司内部使用、打包进商业产品、对外提供付费服务
- 商业使用需另行获得作者授权（[开 issue](https://github.com/Chaierss/workbuddy-trae-assist/issues) 联系）

```
Required Notice: Copyright (c) 2026 NextAgentX (https://github.com/NextAgentX/qoder-trae-workbuddy-switch)
Required Notice: Copyright (c) 2026 Chaierss (https://github.com/Chaierss)
```

Trae 的凭据信封解析（`domain/trae_crypto.py`）与扫码登录（`domain/trae_oauth.py`）
起于对 [qoder-trae-workbuddy-switch](https://github.com/NextAgentX/qoder-trae-workbuddy-switch)
的协议研究——那是同许可的 Rust/Tauri 实现，本项目用 Python 重写。按 PolyForm 的
Notices 条款，上游的 Required Notice 一并保留在上面，不做移除。

---

如果它帮你省下了每天那几趟登出重登，[点个 star](https://github.com/Chaierss/workbuddy-trae-assist/stargazers)
就行——这是我继续维护它的主要动力。有问题
[开 issue](https://github.com/Chaierss/workbuddy-trae-assist/issues)，
想参与开发见 [CONTRIBUTING.md](CONTRIBUTING.md)。

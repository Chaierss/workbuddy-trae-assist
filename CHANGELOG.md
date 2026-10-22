# 更新日志

本文件记录每个版本**对使用者有影响**的变化。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

桌面端会在启动时自动检查更新；也可以看
[Releases](https://github.com/Chaierss/workbuddy-trae-assist/releases) 了解每个版本。

## [未发布]

（下次发版前在这里累积改动，按「新增 / 修复 / 变更」分组；发版时把本节改成版本号与日期。）

## [0.1.2] - 2026-10-22

这一版主要修桌面端：**装上后程序没认出自己是桌面版**，连带设置里的若干入口都不对。

### 修复

- **桌面端读不到「自己是桌面版」。** 根因在 `preload.js`：它在渲染进程里调用
  `require('electron').app.getVersion()`，而 `app` 只存在于主进程，这一行直接抛错，
  导致后面的 `contextBridge.exposeInMainWorld` 整段没有执行，`window.chaierwwDesktop`
  恒为 `undefined`。表现是：设置里本该隐藏的「接口文档」「Wiki 文档」照样出现，
  而「检查更新」入口反而消失——点也点不到。现在版本号由主进程启动时通过
  `webPreferences.additionalArguments` 传给 preload，不再在渲染进程里碰 `app`。
- **凭据里的 `host` 字段可以指向任意域名。** 上游请求地址取自凭据文件的 `host`，
  改动这个字段就能把访问令牌发到别人的服务器。现在只接受 `trae.cn` 及其子域，
  且协议必须是 https，否则回落到官方地址。

### 变更

- **检查更新挪进「设置 → 常规」。** 原先它是一个独立分栏，一年点不了几次却占一个位置。
  现在版本号、检查 / 下载 / 重启按钮都在常规页顶部；下载完成后可以选择立即重启安装，
  选「稍后」则在退出程序时自动安装。
- **托盘右键菜单去掉「检查更新」**，只留「打开 chaierww」和「退出」，更新入口只保留一处。

### 新增

- **「启动时自动检查更新」开关。** 关掉后启动不再静默查询更新。偏好写在
  `%APPDATA%/chaierww/update-prefs.json`，默认开启。
- `SECURITY.md` 新增「上线前自查」一节：发版前的四项检查、危险 API 扫描命令及当前基线、
  信任边界复核要点，以及已修问题记录。

## [0.1.1] - 2026-10-22

**首个公开发布版本。**

### 新增

- **号池与一键切号**：WorkBuddy 国内版 / Trae 国内版账号收在一处，切号从约 60 秒降到 1 秒。
- **切换前预演**：只读地告诉你这次会迁移哪些数据、改动哪些文件。
- **强制备份与自动回滚**：切换前备份，中途失败回滚到切换前快照；每日自动备份，按份数滚动保留 7 份。
- **活动签到与成长任务**：逐账号检测，能自动的一键跑完，不能自动的写明原因。
- **用量统计**：按模型、按日归集请求与积分消耗。
- **额度网关**：把账号额度以 OpenAI / Anthropic 兼容接口开放给本机其它 AI 工具，
  Key 可签发、可吊销，默认只监听 `127.0.0.1:8788`。
- **计划任务**：每日签到、保活探测、全活动执行，支持时点与抖动。执行日志只保留最近
  **7 次**，旧的自动滚掉。
- **自动轮换**：按积分到期时间把默认账号切到最早到期的那个，防止积分过期。
- **桌面端**：Electron 安装包，系统托盘常驻，应用内自动更新。
- **凭据加密**：Windows DPAPI 按当前用户加密，密钥不落文件。
- **「一键执行」首次运行前的风险提示**：成长任务里有 10 项是通过接口把任务标记为完成，
  并非在客户端里真实执行了对应操作。第一次点击时弹窗说明这条链路与「代点签到」的性质差别，
  确认一次后不再打扰。计划任务的自动执行仍不包含成长任务。
- `SECURITY.md`：安全模型、漏洞报告渠道、以及哪些情况不算漏洞。
- Issue 与 PR 模板，附排障所需的最小信息清单。

### 修复

- **Trae 扫码账号签到 100% 失败。** 根因是扫码登录拿到的凭据里 `deviceId` 为空，
  而 `checkin_credits/claim` 把 `x-device-id` 当必填项，缺了就返回 `9004`
  （"The submitted order parameters are incorrect"）。`status` 接口不校验设备头，
  所以界面上看起来一切正常，只有执行时才整批失败。
  现在按账号标识派生一个稳定且唯一的设备号，并对上游的 `9074`（限流）退避重试、
  `9095`（设备名额被占）给出能看懂的提示。
- 计划任务日志里只有「成功 8，失败 7」这种数字，看不出为什么失败。
  现在汇总行会附带失败原因（最多两类，带出现次数）；执行日志里失败的记录也会标红。
- 升级前已存在的「上次结果」会自动补成日志的第一条，不会出现「明明跑过但日志是空的」。
- **桌面端自动更新永远查不到新版**，两个原因都修了：
  ① 发布源仓库名写错（`chaierww` 而非 `workbuddy-trae-assist`），安装包里烧进去的
  `app-update.yml` 指向一个不存在的仓库；② Release 默认建成 draft，而 electron-updater
  只查已发布的版本。
- CI 在 Windows runner 上第一次 print 中文就抛 `UnicodeEncodeError`（控制台代码页不是
  UTF-8），构建第一步即失败。现在 job 级设 `PYTHONIOENCODING=utf-8`，脚本内也做了兜底。
- CI 缺 `contents: write` 权限，安装包打好了但发不出去（`403 Resource not accessible
  by integration`）。

### 变更

- **平台收窄为「只有 Windows 10 / 11 x64」**。macOS / Linux 的包能打出来，但凭据加密
  （DPAPI）与客户端目录定位（`APPDATA` / `LOCALAPPDATA`）只有 Windows 实现，装完启动
  会失败。与其发一个下了用不了的产物，不如不发——CI 构建矩阵只保留 Windows。
- 全部文档重写：README 按「使用者 → 贡献者」重排并去掉重复段落，`desktop/README.md`
  的「三平台」章节改为单平台，`CONTRIBUTING.md` 拆分「必做」与「架构边界」。
- 明确许可证定位：PolyForm Noncommercial 是 **source-available**，不是 OSI 开源许可，
  商业使用需另行授权。
- 重写 `docs/policies/DISCLAIMER.md`：删掉与 `trae_crypto.py` 矛盾的「完全独立开发」表述，
  补上三条硬性使用边界（仅限本人账号、网关不对外提供、不用于账号运营），
  `Required Notice` 补回上游一行——此前只列了作者本人，与 LICENSE 要求不符。

### 说明

- **Windows 10 / 11 x64 为唯一支持的平台。**
- 安装包未做代码签名，首次运行 Windows 会拦一次，需点「更多信息 → 仍要运行」。
- 安装版会带一个后端控制台窗口（`--console`），保留它是为了出问题时能直接看到日志。

## 0.1.0 - 2026-10-22

内部版本，功能与 0.1.1 相同。但文档尚未整理、平台支持描述与实际不符，
发布后即下架，不提供下载。请使用 0.1.1。

[未发布]: https://github.com/Chaierss/workbuddy-trae-assist/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/Chaierss/workbuddy-trae-assist/releases/tag/v0.1.2
[0.1.1]: https://github.com/Chaierss/workbuddy-trae-assist/releases/tag/v0.1.1

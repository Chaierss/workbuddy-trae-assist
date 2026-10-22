# 安全策略

chaierww 会读写本机客户端的登录态、代你向平台发请求、还把额度以 HTTP 接口开放出去。
这三件事都在敏感位置，所以这里写清楚**安全边界在哪、哪些情况不算漏洞**。

## 支持的版本

只有**最新 Release** 接受安全修复。这是本地工具，不存在「还在用旧版的服务端」这种情况，
升级成本只是一次覆盖安装。

## 报告漏洞

**不要开公开 issue。**

请用仓库的私密披露入口：
https://github.com/Chaierss/workbuddy-trae-assist/security/advisories/new
（仓库 **Security** 标签页 → *Report a vulnerability*）

如果入口暂时不可用，开一个 issue **只写「存在安全问题，需要私下渠道」，不要写任何细节**，
维护者会回你一个私下联系方式。

报告里最好带上：影响版本与 Windows 版本、能复现的最小步骤、实际影响（读到了什么 /
写到了什么 / 谁能触发）。已经有修法的话欢迎附上。

确认修复之后再公开披露。我会在 CHANGELOG 里写清楚修了什么，在 Release 说明里致谢。

## 安全模型

设计上做了什么，逐条对应你能验证的位置：

| 措施 | 实现位置 |
| :-- | :-- |
| **数据只在本机** | 全部落在 `~/.chaierww`（`CHAIERWW_HOME` 可覆盖），没有服务端，不上报任何遥测 |
| **凭据加密存储** | `infra/secrets.py`：Windows 走 DPAPI，密钥由系统按当前登录用户派生，**不落文件** |
| **日志脱敏** | 令牌、Cookie、账号 ID 写日志前替换为圆点，日志在 `~/.chaierww/logs/` |
| **网关只对内** | `domain/gateway.py`：默认 `127.0.0.1:8788`，`loopbackOnly` 生效；对外暴露要显式改配置 |
| **网关鉴权** | Key 由本机签发、可随时吊销，不落明文到日志 |
| **写前有备份** | 切换前强制备份，中途失败自动回滚到切换前快照 |
| **只读预演** | 「预演」只读不写，能先看到会改动哪些文件 |
| **不碰程序本体** | 只读写客户端的**状态文件**，不打补丁、不改可执行文件 |
| **备份默认不含凭据** | `GLOBAL_PARTS` 把 `gateway` / `accounts` 标为 `CREDENTIAL_PARTS`，全局备份默认只收 `config`。勾选含凭据分区要显式确认，确认后自动备份沿用该范围（见下条） |
| **导出含凭据，必须显式确认** | `/api/accounts/export` 导出的是**解密后的明文凭据**，拿到文件即可登录这些账号。前端落盘前强制弹确认（说明明文后果 + 禁止发到群/网盘），确认一次后不再逐次打扰 |
| **凭据只在本机闭环** | 导出包与含凭据备份都属于「等价登录态」的产物：不上传、不外发，界面一律提示只留本机 |

## 这些不算漏洞

写下来是为了省掉双方的时间——以下是**已知设计**，不是缺陷：

1. **需要「本机同一用户权限」才能触发的攻击。** DPAPI 的密钥绑定当前 Windows 用户，
   `~/.chaierww` 也归当前用户。已经能以你的身份执行代码的攻击者，直接读客户端自己的
   登录态就行，绕不绕开本工具没有区别。

2. **物理接触 / 已失陷的机器。** 同上。

3. **客户端把文件覆盖回去。** WorkBuddy / Trae 运行时持有登录态，切号前会要求先退出客户端。
   不退出就写会被客户端覆盖——这是两个程序抢同一个文件，不是本工具的漏洞。

4. **安装包没有代码签名。** Windows SmartScreen 会拦一次，README 已说明。没有购买代码签名
   证书是成本取舍，不是包被篡改。要自己校验就下载 Release 里的 `latest.yml`，
   对比其中 `files[0].sha512` 与本地安装包：

   ```powershell
   $b = [IO.File]::ReadAllBytes("chaierww-Setup.exe")
   [Convert]::ToBase64String([Security.Cryptography.SHA512]::Create().ComputeHash($b))
   ```

5. **上游平台的风控、限流、封号。** 本工具只代你操作你自己的账号。触发平台风控属于使用
   风险，见[免责声明](docs/policies/DISCLAIMER.md)。

6. **你自己把网关改成 `0.0.0.0`。** 改配置对外暴露是显式操作，风险自负。

## 不会做的事

无论谁提需求，这几条不会变：

- 不上传账号、凭据、用量到任何服务器
- 不做云端中转、账号托管、账号共享
- 不代收你的密码
- 不修改客户端程序本体
- 不做遥测（连匿名的都不做）

## 依赖与供应链

- **后端**：见 `pyproject.toml`，只有 FastAPI / uvicorn / pydantic / httpx / cryptography / psutil
- **前端与桌面端**：见 `web/package.json`、`desktop/package.json`，锁定在各自的
  `package-lock.json`
- **仓库内的第三方二进制**：`desktop/build/tools/rcedit-x64.exe`（打包时改 exe 资源，
  取自官方 winCodeSign-2.6.0 包，未做修改，仅 Windows 构建用）
- **CI**：跑在 GitHub Actions 上，构建产物不签名，但附 `latest.yml` 供校验
- **依赖漏洞告警**：GitHub 的 Dependabot 漏洞告警与「自动安全修复」都已开启。
  前者被动提示，后者只在有真实安全公告时才开 PR，平时不会制造噪音。
  Python 依赖见 `pyproject.toml`，前端与桌面端靠两处 `package-lock.json`。
  **注意**：告警开关打开后 Dependabot 要几分钟到几小时才扫完第一遍，期间
  「0 告警」是假象——本项目 2026-10-22 首次扫描实际报出 52 条（22 高危），
  根因是 electron 33 已 EOL，升级 43 后清零。每次升级大版本后都该回来看一眼这里。
- **漏洞披露通道**：仓库已开启 Private vulnerability reporting，
  上面「报告漏洞」里的 `/security/advisories/new` 可用。若哪天入口被关，
  按那一节的备用方案走。

这几项都在仓库设置里，**代码里看不出来**，所以记在这里。换维护者或换仓库时，
先去 Settings → Code security 确认它们还是开着的。

## 上线前自查

每次发版前按下面四步过一遍。一、二步跑命令，第三步是人工判断，第四步留个记录——
**不要跳**，本工具碰的是别人的登录态，回归一次代价远高于多花十分钟。

### 一、自动化检查全绿

```bash
.venv/Scripts/python.exe -m pytest -q          # 后端全量，必须 0 failed
cd web && node node_modules/vue-tsc/bin/vue-tsc.js --noEmit
node node_modules/vite/bin/vite.js build
node --check desktop/main.js && node --check desktop/preload.js
```

`node --check` 那行不是形式主义：桌面端的功能全部经 `preload.js` 暴露，
**preload 里任何一行抛错，`exposeInMainWorld` 就整段不执行**，前端会把自己当成普通
浏览器，该隐藏的桌面专属入口全部失效，而主进程日志里通常什么都看不到。

命令之外还有一条要人工看：仓库 **Security → Dependabot** 里，**会打进安装包的依赖**
`high` / `critical` 告警必须为 0 才能发版。

`desktop/package.json` 的 `dependencies` 只有 `electron-updater` 一个，其余（electron、
electron-builder 及其全部子树）都在 `devDependencies`，只跑在构建机上、不进安装包。
所以判定标准是**这个包会不会跟着 asar 走**，不是告警列表干不干净：

```bash
python -c "import json;d=json.load(open('desktop/package-lock.json',encoding='utf-8'));\
[print(k,v.get('version'),'dev=',v.get('dev')) for k,v in d['packages'].items() if '<包名>' in k]"
```

**v0.1.5 核对结论**（照这个格式每次重查，别只看告警总数）：10 条开放告警全是
`scope=development`。其中 `builder-util-runtime` 有两套副本——顶层 9.7.0（electron-updater
的运行时依赖，**已修复**，告警范围是 `< 9.7.0`），其余 9.2.10 副本嵌套在
`app-builder-lib` / `electron-builder` / `dmg-builder` 等构建期包下，`dev: true`，不打包。
`app-builder-lib < 26.15.0` 与 `tar` 同样只在 electron-builder 子树里，只影响构建机。

`medium` 及以下可以排期，但要知道它们存在。

### 二、危险 API 扫描

**必须排除第三方产物**：`src/chaierww/webassets/` 下是 swagger-ui 的压缩包，单行 1.5 MB，
不排除会把输出淹掉，清单就废了。用 `git grep` 而不 `rg`，因为前者随仓库自带、无需额外安装。

```bash
EX=':(exclude)src/chaierww/webassets'
G() { git grep -nE "$1" -- . "$EX" ':(exclude)SECURITY.md' ':(exclude)CHANGELOG.md'; }

G '\beval\(|\bexec\(|pickle\.load|yaml\.load\('            # 动态执行
G 'shell=True|os\.system|popen\('                         # 命令注入
git grep -nE 'v-html|innerHTML' -- web/src                # XSS（前端单独扫）
G "(api[_-]?key|secret|token)[[:space:]]*=[[:space:]]*[\"'][A-Za-z0-9]{16,}"
G '0\.0\.0\.0'                                            # 监听地址
```

排除 `SECURITY.md` / `CHANGELOG.md` 是因为它们会命中自己写的示例命令。

**v0.1.2 自查基线**——下次跑出来比这多，就要逐个看：

| 检查 | 命中 | 判定 |
| :-- | :-- | :-- |
| 动态执行 | `AppShell.vue` 的 `RegExp.exec` | 良性，正则匹配不是求值 |
| 命令注入 | 0 | 唯一的外部进程启动是 `subprocess.Popen([列表])`，无 `shell=True` |
| XSS | `WikiTab.vue` 的 `v-html` | 良性，上游是 `MarkdownIt({ html: false })`，原始 HTML 不注入 |
| 硬编码密钥 | `test_trae_adapter.py` 的拼装 JWT | 良性，测试用假串 |
| 监听地址 | `scripts/shots.py`（日志脱敏）、`ServiceTab.vue`（提示文案）、`test_gateway.py`（**断言被拒**） | 良性，`domain/gateway.py:163` 保证非回环地址必须显式开 `allowNonLoopback` |

### 三、信任边界复核

新增或改动**任何接收外部输入**的代码时，问三个问题：

1. **谁给的？** 用户输入 / 凭据文件 / 上游响应 / 渲染进程 —— 后两者一律按不可信处理。
2. **会不会拼进 URL、路径、命令行？** 拼 URL 要有域名白名单（见
   `domain/platform_api.py` 的 `TRAE_ALLOWED_HOSTS`），拼路径要 `Path.resolve()` 后
   校验前缀，拼命令行只能传列表。
3. **失败时落在哪？** 校验不通过必须回落到**安全默认值**，不能抛出异常把页面打崩，
   也不能「校验失败就放行」。

桌面端另加一条：**新增 IPC 通道时，`preload` 只暴露语义化方法，不要把
`ipcRenderer.invoke` 整个透给渲染进程**——那等于把主进程能力全开放。

### 四、结论写回来

- 动了安全边界（新增白名单、改鉴权、改存储）→ 同步更新上面「安全模型」表格
- 修了漏洞 → 下方「已修问题」记一行，CHANGELOG 写清楚影响范围
- 没发现问题也要在发版 PR / commit 里留一句「已过安全自查」，避免下次不知道查没查

### 已修问题

| 版本 | 问题 | 修法 |
| :-- | :-- | :-- |
| v0.1.2 | 上游请求域名取自凭据里的 `host` 字段，篡改凭据文件可把 JWT 送到任意域 | `platform_api.py` 加 `TRAE_ALLOWED_HOSTS = ("trae.cn",)`，`_safe_trae_origin()` 校验 scheme 必须是 https、主机必须精确匹配或为子域，否则回落官方地址；11 组参数化用例覆盖 `evil.com`、`api.trae.cn.evil.com`、`http://` 等绕过写法 |
| v0.1.2 | `preload.js` 在渲染进程调用 `require('electron').app`，抛错致桌面能力全线失效 | 主进程经 `webPreferences.additionalArguments` 传 `--chaierww-version=`，preload 从 `process.argv` 读取 |

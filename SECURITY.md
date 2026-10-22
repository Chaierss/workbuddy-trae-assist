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

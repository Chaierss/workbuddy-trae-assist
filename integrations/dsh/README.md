# dsh-chaierww

把 chaierww 的号池、用量、切换与活动执行接进 [DeepSeek Harness](https://deepseek-harness.github.io/deepseek-harness/)，
让 DSH 里的模型可以：

| 工具 | 能力 | 写操作 |
| --- | --- | --- |
| `chaierww_pool` | 查号池：分组、备注、凭据状态、积分余额 | 否 |
| `chaierww_usage` | 查用量：今日 / 本周 / 本月的请求与积分 | 否 |
| `chaierww_switch` | 预演或执行切换账号（自动备份、失败回滚） | 是，需 `confirm: true` |
| `chaierww_activities_run` | 执行可自动完成的签到与活动 | 是，需 `confirm: true` |

它是 **host-only 组合包**：没有 client，不新增界面。真正的界面仍在 chaierww 自己的
Web / 桌面端；这个包只把已有能力暴露给模型。

## 安装

先安装并启动 chaierww（默认监听 `127.0.0.1:57910`），然后在本仓库根目录执行：

```sh
dsh plugin --profile web add ./integrations/dsh
```

包名是 `dsh-chaierww`；发布到 npm 后，同一条命令也可以写成
`dsh plugin --profile web add dsh-chaierww`。

`dsh --profile web --dump-config` 应该能看到 `# == dsh-chaierww` 这一层。
卸载：

```sh
dsh plugin --profile web remove dsh-chaierww
```

## 配置

默认值写在 `index.js` 的 Config schema 里，不需要改代码。需要覆盖时，在 profile 的
`cordis.patch.yml` 按行 id 覆盖（patch 会整体替换 `config`，要重述全部键）：

```yaml
- insert:
    - id: chaierww
      name: dsh-chaierww
      config:
        baseUrl: http://127.0.0.1:57910
        tokenFile: ''
        timeoutMs: 15000
```

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `baseUrl` | `http://127.0.0.1:57910` | 只允许回环地址；改成远端会在加载时直接失败 |
| `tokenFile` | 空 | 空 = `$CHAIERWW_HOME/api-token`，未设则 `~/.chaierww/api-token` |
| `timeoutMs` | `15000` | 单次请求超时 |

## 安全边界

- 令牌只发给 `baseUrl`，而 `baseUrl` 在插件加载时校验为 `127.0.0.1` / `localhost` / `::1`，
  避免把本机 API 令牌发给外部主机。
- 请求带 `Authorization: Bearer` 与 `X-Chaierww-Client: web`，走 chaierww 自己的
  `/api/*` 守卫；令牌不进日志、不进工具结果。
- 写操作必须显式 `confirm: true`，未确认直接抛错，不落任何文件。
- 工具结果只回传界面字段与统计，不回传凭据原文。

## 限制

- chaierww 必须在本机运行；连不上会抛错并提示启动，不会返回空号池。
- 切换仍受 chaierww 的平台限制：只覆盖 WorkBuddy 国内版与 Trae 国内版，
  被切换的客户端需要先退出（预演结果里会说明）。
- 活动只代做「可自动 + 未完成」的项；需要真实客户端操作的项仍标手动。

## 文件

| 路径 | 用途 |
| --- | --- |
| `index.js` | 插件入口：Config、四个工具、本机 HTTP 调用 |
| `lib.js` | 纯函数：地址校验、载荷归一、文本渲染 |
| `cordis.patch.yml` | 组合包配置层，按包名挂载插件行 |
| `test.js` | `node --test` 覆盖 `lib.js`，不需要起服务 |

## 开发

```sh
cd integrations/dsh
npm test          # 纯函数单测
npm run verify    # 语法检查 + 单测 + npm pack --dry-run
```

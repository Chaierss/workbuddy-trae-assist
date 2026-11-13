# DSH 插件

把 chaierww 接进 [DeepSeek Harness（DSH）](https://deepseek-harness.github.io/deepseek-harness/)，
让 DSH 里的模型能直接查号池、查用量、预演/执行换号、跑每日活动。

插件是仓库里的组合包 `integrations/dsh`（npm 包名 `dsh-chaierww`），host-only：
没有 client，不新增界面。界面仍在 chaierww 自己的 Web / 桌面端，插件只复用它的
`/api/*`。

## 工具

| 工具 | 做什么 | 是否写 |
| --- | --- | --- |
| `chaierww_pool` | 号池：分组、备注、凭据状态、积分余额 | 只读 |
| `chaierww_usage` | 用量：今日 / 本周 / 本月的请求与积分 | 只读 |
| `chaierww_switch` | 预演或执行切换（备份 + 失败回滚） | 写，需 `confirm: true` |
| `chaierww_activities_run` | 执行可自动完成的签到与活动 | 写，需 `confirm: true` |

「查不了」和「是 0」分开表达：`detectable=false` / `available=false` 会渲染成
「查不了 + 原因」，不会显示成 0 积分或 0 用量。

## 安装

1. 安装并启动 chaierww（默认 `127.0.0.1:57910`，可用 `CHAIERWW_PORT` 改）。
2. 在仓库根目录执行：

```sh
dsh plugin --profile web add ./integrations/dsh
```

包名是 `dsh-chaierww`；发布到 npm 后，同一条命令也可以写成
`dsh plugin --profile web add dsh-chaierww`。

3. 验证插件层已挂上：

```sh
dsh --profile web --dump-config   # 应出现 "# == dsh-chaierww"
```

卸载：`dsh plugin --profile web remove dsh-chaierww`。

## 配置

默认值在 `integrations/dsh/index.js` 的 Config schema 里。需要改时，在 profile 的
`cordis.patch.yml` 按行 id 覆盖（patch 整体替换 `config`，要重述全部键）：

```yaml
- insert:
    - id: chaierww
      name: dsh-chaierww
      config:
        baseUrl: http://127.0.0.1:57910
        tokenFile: ''
        timeoutMs: 15000
```

`tokenFile` 留空时读 `$CHAIERWW_HOME/api-token`，未设 `CHAIERWW_HOME` 则读
`~/.chaierww/api-token`。

## 安全边界

- `baseUrl` 在插件加载时校验：只接受 `127.0.0.1` / `localhost` / `::1`，
  非回环地址直接拒绝启动，避免把本机 API 令牌发给外部主机。
- 每次请求带 `Authorization: Bearer` 与 `X-Chaierww-Client: web`，走 chaierww 自己的
  `/api/*` 守卫；令牌不写日志、不进工具结果。
- 写操作必须显式 `confirm: true`；未确认直接抛错，不落任何文件。
- 工具结果只回传界面字段与统计，不回传凭据原文。

## 限制

- chaierww 必须在本机运行；连不上会抛错并提示启动，不返回空号池。
- 平台范围与 chaierww 一致：只覆盖 WorkBuddy 国内版与 Trae 国内版。
- 切换仍需先退出被切换的客户端（预演结果会写明）；活动只代做「可自动 + 未完成」的项。

## 开发与门禁

```sh
cd integrations/dsh
npm test          # 纯函数单测（不起服务）
npm run verify    # 语法检查 + 单测 + npm pack --dry-run
```

`lib.js` 只放纯函数（校验 / 归一 / 渲染），网络与令牌读取留在 `index.js`，
这样单测不需要 chaierww 在跑。

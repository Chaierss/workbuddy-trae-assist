# 本地体验数据说明

chaierww 的度量**只写本机、不上传**。这一页说明记了什么、存在哪、怎么关、怎么删。

---

## 一句话

为了判断「新装用户能不能顺利换出第一个号」，工具会在本机记几条匿名计数。
不联网、不引第三方统计 SDK、不建服务端。默认开启，随时可关，关掉立即停止写入。

---

## 记了什么

只有**事件名 + 白名单属性 + 时间**，共 7 个事件：

| 事件 | 含义 | 允许的属性 |
|:--|:--|:--|
| `app_open` | 打开应用（`first=true` 标记安装后首次） | `first` |
| `client_detected` | 检测到客户端状态 | `status`（missing / installed / logged_in / running） |
| `onboarding_step` | 引导清单完成某一步 | `step` |
| `account_imported` | 成功加入一个账号 | `method`（scan / bundle / json） |
| `switch_previewed` | 点了切换预演 | — |
| `switch_completed` | 切换成功 | — |
| `gateway_key_created` | 签发网关 Key | `method` |

属性白名单共 6 个键：`locale`、`platform`、`step`、`status`、`method`、`first`。
字符串值超过 32 字符直接丢弃。

---

## 不记什么

- **账号**：账号名、账号 ID、手机号、邮箱、所属套餐。
- **凭据**：token、cookie、任何登录态内容。
- **内容**：对话正文、prompt、活动执行结果正文。
- **环境**：文件路径、主机名、IP、设备指纹、安装 ID。
- **任何可自由填写的文本字段。**

也就是说，这份数据无法反查到「是谁、用了哪个号、聊了什么」。

---

## 存在哪

```
~/.chaierww/metrics/events.ndjson      # 当前文件
~/.chaierww/metrics/events.1.ndjson    # 轮转备份
~/.chaierww/metrics/events.2.ndjson    # 更早的一份
```

- 每行一条 JSON，追加写入。
- 单文件满 5 MB 轮转一次，最多保留 2 份历史后丢弃最旧的一份，长期使用也不会占满磁盘。
- `~/.chaierww` 可用环境变量 `CHAIERWW_HOME` 覆盖。
- 写入失败只记日志，**不会阻断任何操作**。

---

## 漏斗口径

「概览」用的是本机汇总，不是云端大盘：

- **首次打开**：`app_open(first=true)`；
- **首次换号**：第一条 `switch_completed`；
- **TTFV**（Time To First Value）：两者之差，取多次样本的中位数；
- **激活**：首次换号发生在首次打开后 **24 小时内**；
- **引导完成率**：`onboarding_step` 覆盖到 `retention` 的比例。

样本小、偏差大，只作方向判断；对外数据以下载量、Star 与 issue 为准。

---

## 怎么关、怎么导出、怎么删

打开「设置 → 环境 → 体验数据」：

- **开关**：关闭后前端立即停止上报，后端静默丢弃事件（接口仍返回 204，不报错）。
  偏好写在 `preferences.json` 的 `localMetricsEnabled`。
- **导出 JSON**：把当前事件、汇总与漏斗一次性下载到本地，便于自己核对。
- **清空**：删除全部 `events*.ndjson`，不可恢复，需要二次确认。

对应的本机接口（仅供本页面使用，走同源守卫）：

```
POST   /api/metrics/events          # 记一条事件（关闭时静默丢弃）
GET    /api/metrics/funnel?days=14  # 本机漏斗汇总
GET    /api/metrics/export          # 导出原始事件 + 汇总
DELETE /api/metrics                 # 清空
```

---

## 与云端的关系

没有云端。这个仓库也没有配置任何统计上报端点；把 `~/.chaierww` 整个删掉，
这份数据就彻底不存在了。

相关：[快速上手](QUICKSTART.md) · [免责声明](DISCLAIMER.md) · [安全模型](../../SECURITY.md)

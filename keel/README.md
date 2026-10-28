# keel · 项目唯一真相源

这个目录是 **Keel（龙骨）** 的底座：设置页的「规则 / 组件 / 接口文档 / Wiki」四件套全部从这里取事实，
两个零依赖脚本把「四件套是否完整」「上线前是否踩雷」钉成门禁。新 agent 接手项目，先读这里，再读代码。

## 里面有什么

| 文件 | 是什么 | 谁消费 |
|---|---|---|
| `console.config.json` | 扫描范围（源码目录、组件目录、wiki 目录、排除项）+ 模板版本号 | 体检脚本 |
| `rules.json` | 项目规则 + 页面形态术语（`title`/`summary`/`detail` 存 i18n key） | 设置 → 规则；导出给 AI |
| `components.json` | 前端组件注册表（准入名单，不是说明书） | 设置 → 组件；体检脚本；`virtual:keel-report` |
| `wiki.json` | 门禁清单：`docs/` 下每篇 `.md` 登记一行（校验存在、非空、无漏登记） | 体检脚本 |
| `console-check.mjs` | 四件套完整性体检（`CFG` / `VER` / `RULE` / `COMP` / `WIKI`） | CI / 本地提交前 |
| `preflight-scan.mjs` | 上线前静态漏洞扫描（17 条规则 + `.preflightignore`） | CI / 发布门禁 |

接口文档**不在这里**：真相源是后端路由与类型，运行期由 FastAPI 生成 `/api/openapi.json`，
设置 → 接口文档页直接读取渲染，不打包任何预生成副本。

Wiki 也不在 `wiki.json` 里维护正文：设置 → Wiki 由后端运行期读 `docs/` 目录，新增 `.md` 刷新即出现；
`wiki.json` 只用于门禁校验「清单里每篇都存在、磁盘上没有漏登记」。对外公开的白名单由
`scripts/check_public_docs.py` 单独负责，两者不要混用。

## 三条不能破的约定

1. **改内容只改这里的文件**，设置页不写任何文案副本。页面与这里不一致，视为 bug。
2. **新增组件必须登记 `components.json`**，否则体检脚本报错、设置页出现「未登记」告警条。
3. **改了源文件刷新就能看到**。需要「点一下同步」才生效，说明管道断了 —— 去修管道，不要加同步按钮。

## 日常命令

```bash
node keel/console-check.mjs           # 四件套体检
node keel/console-check.mjs --strict  # CI / 提交前：error 必须为零
node keel/console-check.mjs --json    # 给 CI 或页面消费

node keel/preflight-scan.mjs          # 漏洞扫描报告
node keel/preflight-scan.mjs --strict # 发布前：error 必须为零
node keel/preflight-scan.mjs --include-dist  # 连构建产物一起扫，查前端泄露密钥
```

`.preflightignore` 的每一条豁免都必须写明原因；没有原因的豁免等于关掉检测。

**发布顺序**：四件套体检 → 漏洞扫描 → 依赖审计 → 人工五项（越权 IDOR / 权限只在前端 / 逻辑绕过 / 上传 / SSRF）→ 产物扫描。顺序不可颠倒。

## 升级技能后怎么迁移

这套目录是**拷贝制**：技能本体（全局 `skills/keel`）更新了，这里不会自动跟着变，两者只靠
`console.config.json` 的 `keelVersion` 对账。`node keel/console-check.mjs` 的 `VER-01` 会给三种状态：

| 输出 | 含义 |
|---|---|
| `ok 模板版本 2.1.0（最新）` | 对齐了 |
| `warn 模板版本 2.0.0，当前技能 2.1.0` | 有更新，按技能里的 `references/upgrading.md` 逐段迁移 |
| `warn 未声明模板版本` | 老文件缺字段，补 `"keelVersion"` |

它是 **warn 不是 error**：升级提醒不该让 CI 变红，红的东西会被整体忽略。

迁移口径：**别整目录覆盖**。`rules.json` / `components.json` / `wiki.json` 是本项目事实，
工具无权判断该留哪条；只搬脚本与本项目相关的配置项，搬完自己跑一遍体检和扫描。

## 加东西的流程

**加一条规则** → 改 `rules.json` 的 `rules` 数组 → 刷新设置页。`title`/`summary`/`detail` 填 i18n key，
并在 `web/src/lib/locales/settings.ts` 补中英两份文案。规则要变就走代码评审，页面只读。

> 两条必带：**P-01 设计优雅原则**（判定「什么叫好」）与 **S-01 上线前安全检查**（判定「什么时候能发」）。

**加一个术语** → 改 `rules.json` 的 `pageModes`。四个字段别省：`definition`（是什么）、
`signal`（怎么一眼认出）、`avoid`（最容易混的说法）。

**加一个组件** → 在 `web/src/components/` 下写组件 → `components.json` 加一条（`path` 相对仓库根）
→ 在 `locales/settings.ts` 补 `summaryKey` 文案 → 刷新设置页。目录里有、注册表没有会被直接点名。

**加一篇 wiki** → 在 `docs/` 下写 `.md` → 设置页刷新即可见；若要对外公开，再在 `wiki.json` 加一行。

## 为什么用 JSON 而不是 TS

跨 agent、跨语言可读。新接手的 agent 不需要理解 Vue / Python 就能读懂约束，脚本也不用解析源码。
`web/src/lib/*.ts` 只做一层薄包装收敛类型，真相源始终是 JSON。

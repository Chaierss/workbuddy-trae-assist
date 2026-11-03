# AGENTS.md

> 跨工具入口，只做路由，不搬内容。上限 60 行——超过就该删，细节在 `keel/` 与 `docs/`。

## 开工前按顺序读

1. `keel/README.md` — 唯一真相源目录：规则 / 组件 / Wiki 清单 / 门禁脚本
2. `keel/project.json` — 目标、范围外、里程碑与出口条件
3. `keel/structure.json` — 顶层目录职责；新增目录先在这里登记
4. `docs/README.md` — 文档入口，按快速开始 → 功能 → 部署分流

## 五条硬约定

1. 内容只改 `keel/` 与 `docs/` 里的文件，设置页不写第二份文案。
2. 新增共享组件：先登记 `keel/components.json`，再在同目录加 `*.demo.*`。
3. 新增 Wiki：写 `docs/*.md`，并在 `keel/wiki.json` 加一行。
4. 新增顶层目录：先在 `keel/structure.json` 写职责，再建目录。
5. 改了源文件刷新就该看到；需要「点同步」说明管道断了，去修管道，别加同步按钮。

## 提交前

```bash
node keel/console-check.mjs --strict
node keel/preflight-scan.mjs --strict
.venv/Scripts/python.exe -m ruff check src tests
.venv/Scripts/python.exe -m pytest -q
npm run build --prefix web
```

发布顺序：四件套体检 → 漏洞扫描 → 依赖审计 → 人工五项 → 产物扫描 → 打 tag。

## 不要做

- 不另建文档站、不在页面里手写规则副本。
- 不用 `NODE_ENV` 或 `?mode=dev` 判可见性，不做模式角标或切换开关。
- 不整目录覆盖 `keel/` 升级——它是拷贝制，覆盖会冲掉项目事实。
- 不在 `AGENTS.md` 里堆结构细节；这里只放路由。

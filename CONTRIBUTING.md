# 贡献指南

## 许可前提

本项目采用 [PolyForm Noncommercial License 1.0.0](LICENSE)。**提交任何贡献即表示你同意
将贡献按同一许可授权给项目作者**，作者可将其包含在未来的任何版本（含未来的再许可决定）中。

LICENSE 开头有两行 `Required Notice`：第一行是上游
[qoder-trae-workbuddy-switch](https://github.com/NextAgentX/qoder-trae-workbuddy-switch)
（Trae 凭据与扫码协议的参考实现），第二行是本项目。**两行都不能删**，
重新品牌化或换语言重写都不构成删除理由。

如果你从别的项目搬了代码进来，同样要在 LICENSE 顶部补一行它的 `Required Notice`——
这是许可的硬要求，不是礼节。

## 从源码跑起来

仓库是完整的，没有需要另外获取的私有包：

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows；macOS/Linux 用 source .venv/bin/activate
pip install -e ".[dev]"

python -m chaierww.main           # 起来后访问 http://127.0.0.1:57910/
```

`src/chaierww/` 的分层在包 docstring 里写了：`adapters/` 认登录态文件结构，
`domain/switching.py` 算切换计划并落盘，`infra/secrets.py` 管凭据加密，
`api/` 是桌面端与 Web 前端共用的本地 HTTP 接口。任何一层都可以直接改。

## 提交前

必做的两件：

```bash
pytest              # 508 个用例应全绿
ruff check src tests
```

**改到用户可见行为就更新 `CHANGELOG.md`**，写在 `[未发布]` 下面，按「新增 / 修复 / 变更」
分类。用使用者的语言写——写「签到失败的账号不再报错」，不要写「重构 `_run_trae` 分支」。

如果动到切换引擎或凭据加密，请在 PR 描述里写明测试方式，至少跑过
`scripts/drill_switch.py`。

## 不要越过这几条

README 里「数据与安全」那张表（不做云端中转、不做账号托管、不代登录、不改客户端本体、
不上报数据、不碰平台风控）是刻意的架构决定。提 PR 改动其中任何一条会被拒绝，
不是评审口味问题。

**新客户端适配的纪律**：状态文件路径必须**经过实测**，没实测过的路径一律拒绝写。
`adapters/base.py` 里的 slot 设计就是为此——`NotInstalled` / `StateUnreadable` 要老实抛，
不要为了让测试过而兜底成空值。宁可报「读不到」，也不要猜一个路径然后写坏别人的登录态。

## 文档放哪

`docs/` 默认不公开（`.gitignore` 白名单只放行 `docs/policies/DISCLAIMER.md`），
里面的 `.md` 加了也进不了仓库，`repo-guard` CI 会直接报红。

想写文档请写进 PR 描述或 issue。确实需要长期保留的，先在 issue 里说明为什么它该公开，
再改 `.gitignore` 白名单并同步 `scripts/check_public_docs.py`。

`docs/images/*.png` 是例外——README 直接引用，可以入库。

## 推送纪律

只往 `origin` 推 `main`（发版时推 `v*` tag）。`master` / `backup/*` 是本地历史备份，
里面留着早年的内部文档，`git push --all` 会把它们一起推到公开仓库。

仓库自带 `.githooks/pre-push`：拒绝非 main 分支，并在推送前跑一遍
`scripts/check_secrets.py`（日志、数据库、备份、假密钥都会被拦下）。新克隆的机器
需要手动接线一次：

```bash
git config core.hooksPath .githooks
```

钩子只是本地闸门，能被 `--no-verify` 绕过；CI 里的 `repo-guard` 是第二道。

## 提交信息

格式 `类型: 摘要`，`feat` / `fix` / `docs` / `chore` / `refactor` / `test`，中英文均可。
一个 PR 聚焦一件事，避免大杂烩。

## 安全漏洞

**不要在公开 issue 里报告安全漏洞**（凭据处理、路径写入、网关绕过等）。

1. 优先用仓库 **Security** 标签页的 *Report a vulnerability*（私密披露，只有维护者可见）
2. 该入口没开时，开一个 issue **只写「存在安全问题，需要私下渠道」，不要写任何细节**

完整的安全模型、以及**哪些情况不算漏洞**，见 [SECURITY.md](SECURITY.md)。
确认修复之后再公开披露。

## 更新截图

修改界面后如需更新 README 截图，直接跑截图脚本（脱敏在截图前就在页面里做完了）：

```bash
python -m chaierww.main                  # 另开终端
pip install playwright pillow
python scripts/shots.py --theme dark     # 输出到 docs/images/
```

脚本会先把账号名、账号 ID、本机路径、邮箱替换成圆点，把首字母头像模糊掉，再截图。
**提交前请自己再扫一眼 `docs/images/` 下的图**，确认没有遗漏的个人信息。
脱敏不能指望「反正没人看」。

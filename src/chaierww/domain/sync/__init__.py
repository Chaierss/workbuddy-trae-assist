"""多账号会话同步（一期）。

只覆盖 WorkBuddy 的本地会话数据；Trae 无本地会话库，能力矩阵里如实标
``unavailable``。登录态与网页版态的代际由 ``webview_state`` / 账号库承担，
本包只负责「会话镜像 + 切换点补齐」。

模块划分：

- :mod:`canonical`：血统稳定键、正文规范化（把 cid 换成稳定键）、sha256；
- :mod:`store`：内容寻址（CAS）的 blob 存取与 GC；
- :mod:`repository`：``sync_state`` / ``sync_items`` / ``sync_jobs`` 的读写；
- :mod:`engine`：采集 / 应用 / 冲突 / 报告。
"""

from chaierww.domain.sync.engine import SyncEngine, SyncReport

__all__ = ["SyncEngine", "SyncReport"]

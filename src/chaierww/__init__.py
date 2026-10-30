"""chaierww：本地多账号切换工具。

把「现在登的是哪个号」变成一次点击。四个平台适配器（WorkBuddy / Trae 等）
各自知道登录态存在哪、怎么原子替换，切换引擎负责干跑预演 → 备份 → 写入 → 校验 → 回滚。

包内分层：

- ``adapters/`` —— 客户端登录态的文件结构。逆向得出，平台一改就会失效，是维护成本最高的一层
- ``domain/switching.py`` —— 切换引擎。先算一遍计划再落盘，中途任何一步对不上就整体回滚
- ``domain/accounts/`` —— 账号仓储，切换引擎读写账号走这里，不另存一份真相
- ``infra/secrets.py`` —— 凭据加密。Windows 走 DPAPI 按当前用户派生密钥，Linux 走
  AES-256-GCM 按 machine-id + uid 派生，两种都不落文件
- ``api/`` —— 本地 HTTP 网关，桌面端前端与小程序都通过它访问
"""

from __future__ import annotations

__version__ = "0.12.0"

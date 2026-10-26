"""会话正文的规范化：让同一条血统在任何账号下字节一致。

## 为什么需要

会话正文 ``{cid}.jsonl`` 里嵌着会话 id。直接复制到另一个账号时必须换 id，
换完之后字节就不一样了 —— 于是「同一份内容」无法去重、也无法用哈希校验。

这里把 cid 换成**血统稳定键**（stableKey）再存镜像；应用时再换回目标账号的
本地 cid。占位符是 32 位十六进制，正常正文里不会出现，替换可逆。

## 稳定键

``stableKey = sha256(scope | rootUid | rootCid)[:32]``

``rootUid/rootCid`` 是血统根（见 ``session_migration.lineage_root``），
所以 A→B→C→A 往返链路上所有副本共享同一个键。
"""

from __future__ import annotations

import hashlib

#: 占位符前缀，便于人眼排查（真正的键是后面 32 位 hex）。
STABLE_PREFIX = "sk-"


def stable_key(scope: str, root_uid: str, root_cid: str) -> str:
    """血统稳定键：同一根血统在任何账号、任何方向上都得到同一个值。"""
    raw = f"{scope}\x1f{root_uid}\x1f{root_cid}".encode()
    return STABLE_PREFIX + hashlib.sha256(raw).hexdigest()[:32]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def canonicalize(text: str, cid: str, key: str) -> str:
    """把正文里的 ``cid`` 换成稳定键。经 ``localize`` 可逆。"""
    if not text or not cid:
        return text
    return text.replace(cid, key)


def localize(text: str, key: str, cid: str) -> str:
    """把镜像里的稳定键换回目标账号的本地 cid。"""
    if not text or not key:
        return text
    return text.replace(key, cid)


def line_count(text: str) -> int:
    """非空行数。空正文返回 0，末尾有无换行都不影响计数。"""
    if not text:
        return 0
    return sum(1 for line in text.splitlines() if line.strip())

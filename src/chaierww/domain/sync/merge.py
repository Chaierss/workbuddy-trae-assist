"""会话正文的时间线合并。

## 为什么不是「按 timestamp 重排」

WorkBuddy 的 ``{cid}.jsonl`` 不是平铺消息列表：``message`` 下面挂着
``function_call`` / ``function_call_result`` / ``reasoning``，靠 ``id`` +
``parentId`` 组成一条链。按时间戳全局排序会把一条消息和它的工具调用拆散。

## 合并粒度

只合并「共同祖先之后的两段分支」，不拆分支内部的行：

1. 按链节点（``message`` / ``function_call`` / ``function_call_result`` /
   ``reasoning`` / ``credit-usage``）求两边的共同前缀；
2. 前缀一致 = 同一血统，两段分支各自保留原始顺序；
3. 先发生的分支在前，后发生的分支整体接在它后面，并把后一段的首个链节点
   的 ``parentId`` 挂到前一段的最后一个链节点；
4. 元数据行（``session-meta`` / ``ai-title`` / ``file-history-snapshot``）
   留在各自分支的原位置，重复的只保留一份。

这样两边各聊了一段时，结果是「先 A 的续写，再 B 的续写」，而不是两个冲突副本；
每条消息仍然在自己的工具调用之前，父子链不会断。

## 回退

以下情况返回 ``ok=False``，由调用方走原来的「冲突副本」：
找不到共同前缀、链节点缺 ``id``、同一 ``id`` 内容不同、首个链节点没有时间戳、
合并结果出现重复 ``id``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

#: 参与父子链的节点类型。其余（session-meta / ai-title / file-history-snapshot /
#: 未知类型）按元数据处理，不参与共同前缀计算。
CHAIN_TYPES = frozenset(
    {"message", "function_call", "function_call_result", "reasoning", "credit-usage"}
)


@dataclass(frozen=True, slots=True)
class MergeAttempt:
    ok: bool
    text: str = ""
    strategy: str = ""  # same | append | local_newer | timeline
    reason: str = ""


@dataclass(frozen=True, slots=True)
class _Node:
    index: int
    raw: str
    data: dict
    kind: str
    node_id: str
    ts: int | None
    norm: str
    content_norm: str

    @property
    def is_chain(self) -> bool:
        return self.kind in CHAIN_TYPES


def _norm(data: dict) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _parse(text: str) -> list[_Node] | None:
    nodes: list[_Node] = []
    for index, raw in enumerate(text.splitlines()):
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        raw_id = data.get("id")
        timestamp = data.get("timestamp")
        nodes.append(
            _Node(
                index=index,
                raw=raw,
                data=data,
                kind=str(data.get("type") or ""),
                node_id=str(raw_id) if raw_id else "",
                ts=timestamp if isinstance(timestamp, int) else None,
                norm=_norm(data),
                content_norm=_norm(
                    {key: value for key, value in data.items() if key != "parentId"}
                ),
            )
        )
    return nodes


def _chain(nodes: list[_Node], side: str) -> tuple[list[_Node], str]:
    """取链节点并校验 id 唯一；同一 id 内容不同视为真冲突。"""
    chain: list[_Node] = []
    seen: dict[str, str] = {}
    for node in nodes:
        if not node.is_chain:
            continue
        if not node.node_id:
            return [], f"{side}有链节点缺少 id"
        previous = seen.get(node.node_id)
        if previous is not None and previous != node.norm:
            return [], f"{side}的节点 {node.node_id} 出现两种内容"
        if previous is None:
            seen[node.node_id] = node.norm
            chain.append(node)
    return chain, ""


def _common_prefix(a: list[_Node], b: list[_Node]) -> int:
    limit = min(len(a), len(b))
    index = 0
    while index < limit and a[index].node_id == b[index].node_id and a[index].norm == b[index].norm:
        index += 1
    return index


def _first_ts(branch: list[_Node]) -> int | None:
    for node in branch:
        if node.ts is not None:
            return node.ts
    return None


def _last_chain_id(branch: list[_Node]) -> str:
    for node in reversed(branch):
        if node.is_chain:
            return node.node_id
    return ""


def _reparent(node: _Node, parent_id: str) -> str:
    """只改 ``parentId``，其余字段保持原样。"""
    if not parent_id or node.data.get("parentId") == parent_id:
        return node.raw
    data = dict(node.data)
    data["parentId"] = parent_id
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _prefix_text(nodes: list[_Node], ancestor_index: int) -> str:
    return "\n".join(node.raw for node in nodes if node.index <= ancestor_index)


def _branch_lines(nodes: list[_Node], ancestor_index: int) -> list[_Node]:
    return [node for node in nodes if node.index > ancestor_index]


def _render(
    prefix: list[str],
    prefix_meta: set[str],
    first_branch: list[_Node],
    second_branch: list[_Node],
    ancestor_id: str,
) -> str:
    """前缀 + 先发生的分支 + 后发生的分支；后一段首节点挂到前一段末尾。"""
    seen_meta = set(prefix_meta)
    out: list[str] = list(prefix)

    first_chain = next((node for node in first_branch if node.is_chain), None)
    for node in first_branch:
        if node is first_chain and ancestor_id:
            out.append(_reparent(node, ancestor_id))
            continue
        if not node.is_chain:
            if node.norm in seen_meta:
                continue
            seen_meta.add(node.norm)
        out.append(node.raw)

    second_chain = next((node for node in second_branch if node.is_chain), None)
    anchor = _last_chain_id(first_branch) or ancestor_id
    for node in second_branch:
        if node is second_chain and anchor:
            out.append(_reparent(node, anchor))
            continue
        if not node.is_chain:
            if node.norm in seen_meta:
                continue
            seen_meta.add(node.norm)
        out.append(node.raw)
    return "\n".join(out)


def merge_timeline(local: str, mirror: str) -> MergeAttempt:
    """把本地分支与镜像分支按时间线合到一份正文。

    入参都是**规范化后的正文**（cid 已换成 stableKey）。成功时返回的
    ``text`` 也是规范化正文，调用方负责 ``localize`` 后写盘。
    """
    if local == mirror:
        return MergeAttempt(ok=True, text=local, strategy="same")
    if _is_line_prefix(local, mirror):
        return MergeAttempt(ok=True, text=mirror, strategy="append")
    if _is_line_prefix(mirror, local):
        return MergeAttempt(ok=True, text=local, strategy="local_newer")

    local_nodes = _parse(local)
    mirror_nodes = _parse(mirror)
    if local_nodes is None or mirror_nodes is None:
        return MergeAttempt(ok=False, reason="正文不是合法 JSONL")

    local_chain, error = _chain(local_nodes, "本地")
    if error:
        return MergeAttempt(ok=False, reason=error)
    mirror_chain, error = _chain(mirror_nodes, "镜像")
    if error:
        return MergeAttempt(ok=False, reason=error)

    local_ids = {node.node_id for node in local_chain}
    mirror_ids = {node.node_id for node in mirror_chain}
    if not local_ids or not mirror_ids:
        return MergeAttempt(ok=False, reason="正文没有可识别的会话链")
    mirror_by_id = {node.node_id: node.content_norm for node in mirror_chain}
    for node in local_chain:
        if node.node_id in mirror_by_id and mirror_by_id[node.node_id] != node.content_norm:
            return MergeAttempt(ok=False, reason=f"节点 {node.node_id} 两边内容不同")
    if local_ids <= mirror_ids:
        # 本地是镜像的子集：镜像已经是「两边合并后」的版本，直接补齐即可。
        return MergeAttempt(ok=True, text=mirror, strategy="append")
    if mirror_ids <= local_ids:
        return MergeAttempt(ok=True, text=local, strategy="local_newer")

    prefix_len = _common_prefix(local_chain, mirror_chain)
    if prefix_len == 0:
        return MergeAttempt(ok=False, reason="两边找不到共同的链前缀")
    if prefix_len == len(local_chain):
        return MergeAttempt(ok=True, text=mirror, strategy="append")
    if prefix_len == len(mirror_chain):
        return MergeAttempt(ok=True, text=local, strategy="local_newer")

    local_ancestor = local_chain[prefix_len - 1]
    mirror_ancestor = mirror_chain[prefix_len - 1]
    local_branch = _branch_lines(local_nodes, local_ancestor.index)
    mirror_branch = _branch_lines(mirror_nodes, mirror_ancestor.index)

    local_ts = _first_ts([node for node in local_branch if node.is_chain])
    mirror_ts = _first_ts([node for node in mirror_branch if node.is_chain])
    if local_ts is None or mirror_ts is None:
        return MergeAttempt(ok=False, reason="分叉段缺少时间戳")

    if local_ts <= mirror_ts:
        first_branch, second_branch = local_branch, mirror_branch
    else:
        first_branch, second_branch = mirror_branch, local_branch

    prefix = _prefix_text(local_nodes, local_ancestor.index).splitlines()
    prefix_meta = {
        node.norm
        for node in local_nodes
        if node.index <= local_ancestor.index and not node.is_chain
    }
    text = _render(
        prefix,
        prefix_meta,
        first_branch,
        second_branch,
        local_ancestor.node_id,
    )
    merged = _parse(text)
    chain_ids = [
        node.node_id for node in merged or [] if node.is_chain and node.node_id
    ]
    if merged is None or not text.strip() or len(chain_ids) != len(set(chain_ids)):
        return MergeAttempt(ok=False, reason="合并结果出现重复节点")
    return MergeAttempt(
        ok=True,
        text=text,
        strategy="timeline",
    )


def _is_line_prefix(shorter: str, longer: str) -> bool:
    """按行判断前缀，避免把半个 JSON 行当成共同前缀。"""
    if shorter == longer:
        return True
    if not longer.startswith(shorter):
        return False
    if not shorter:
        return True
    return shorter.endswith("\n") or longer[len(shorter)] == "\n"

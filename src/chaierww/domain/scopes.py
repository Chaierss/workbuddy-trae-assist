from __future__ import annotations

from dataclasses import dataclass


class UnknownScope(ValueError):
    """未知作用域。

    解析必须 fail-closed：宁可报错，也不能回落到某个默认作用域，
    否则会把账号读写到错误的库里。
    """


class ScopeNotImplemented(NotImplementedError):
    pass


@dataclass(frozen=True, slots=True)
class Scope:
    key: str
    platform: str
    region: str
    label: str
    implemented: bool


#: 作用域必须满足两条才可加入：① 产品线有**独立**登录态目录；② 只读探针验证通过。
#: 共用同一登录态目录的产品线不能拆成两个作用域 —— 切换会互相覆盖（见
#: `.trae/documents/platform-probe-2026-11-02.md`）。
_SCOPES: tuple[Scope, ...] = (
    Scope("workbuddy-cn", "workbuddy", "cn", "WorkBuddy 国内版", True),
    Scope("trae-cn", "trae", "cn", "Trae 国内版", True),
)

_BY_KEY = {scope.key: scope for scope in _SCOPES}


def all_scopes() -> tuple[Scope, ...]:
    return _SCOPES


def get_scope(key: str | None) -> Scope:
    if not key or not isinstance(key, str):
        raise UnknownScope("作用域不能为空")
    scope = _BY_KEY.get(key)
    if scope is None:
        raise UnknownScope(f"未知作用域：{key}")
    return scope


def require_implemented(key: str) -> Scope:
    scope = get_scope(key)
    if not scope.implemented:
        raise ScopeNotImplemented(f"作用域 {scope.label} 尚未实现")
    return scope

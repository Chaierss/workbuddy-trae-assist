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


_SCOPES: tuple[Scope, ...] = (
    Scope("workbuddy-cn", "workbuddy", "cn", "WorkBuddy 国内版", True),
    Scope("workbuddy-global", "workbuddy", "global", "WorkBuddy 国际版", False),
    Scope("trae-cn", "trae", "cn", "Trae 国内版", True),
    Scope("trae-global", "trae", "global", "Trae 国际版", False),
    Scope("qoder-cn", "qoder", "cn", "Qoder 国内版", False),
    Scope("qoder-global", "qoder", "global", "Qoder 国际版", False),
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

"""客户端本地端口：只读枚举，不连接、不探测。"""

from __future__ import annotations

from types import SimpleNamespace

import psutil

from chaierww.domain.client_ports import local_listening_ports


def _proc(pid: int, name: str, exe: str = "") -> SimpleNamespace:
    return SimpleNamespace(info={"pid": pid, "name": name, "exe": exe})


def _conn(pid: int, port: int, *, listen: bool = True, ip: str = "127.0.0.1"):
    return SimpleNamespace(
        pid=pid,
        status=psutil.CONN_LISTEN if listen else psutil.CONN_ESTABLISHED,
        laddr=SimpleNamespace(port=port, ip=ip),
    )


def test_only_client_listen_ports_are_returned(monkeypatch):
    procs = [_proc(10, "WorkBuddy.exe"), _proc(11, "chrome.exe")]
    conns = [
        _conn(10, 12345),
        _conn(10, 12345),  # 同一端口重复出现只算一次
        _conn(10, 54321, listen=False),
        _conn(11, 8080),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter(procs))
    monkeypatch.setattr(psutil, "net_connections", lambda kind: conns)

    result = local_listening_ports(
        lambda name, _exe: "workbuddy" in name.lower()
    )
    assert result == {
        "available": True,
        "ports": [{"port": 12345, "address": "127.0.0.1"}],
        "reason": "",
    }


def test_not_running_returns_empty_without_scanning_ports(monkeypatch):
    monkeypatch.setattr(
        psutil, "process_iter", lambda attrs: iter([_proc(11, "chrome.exe")])
    )

    def _fail(_kind: str):
        raise AssertionError("客户端未运行时不应枚举端口")

    monkeypatch.setattr(psutil, "net_connections", _fail)
    result = local_listening_ports(lambda _name, _exe: False)
    assert result["available"] is True
    assert result["ports"] == []
    assert result["reason"] == "客户端未运行"


def test_permission_denied_degrades(monkeypatch):
    def _deny(_attrs):
        raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, "process_iter", _deny)
    result = local_listening_ports(lambda _name, _exe: True)
    assert result["available"] is False
    assert result["ports"] == []
    assert "权限不足" in result["reason"]


def test_port_scan_permission_denied_degrades(monkeypatch):
    monkeypatch.setattr(
        psutil, "process_iter", lambda attrs: iter([_proc(10, "WorkBuddy.exe")])
    )

    def _deny(**_kwargs):
        raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, "net_connections", _deny)
    result = local_listening_ports(lambda _name, _exe: True)
    assert result["available"] is False
    assert "权限不足" in result["reason"]

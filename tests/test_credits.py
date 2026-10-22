"""积分查询（解析 + 凭据可查性 + 接口）测试。"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from chaierww.domain.credits import (
    CreditInfo,
    CreditsService,
    CreditUnavailable,
    parse_trae_packages,
    parse_trae_packs,
    parse_wb_resources,
)
from chaierww.domain.platform_api import (
    trae_credentials,
    workbuddy_credentials,
    workbuddy_origin,
)

NOW = 1_700_000_000


# ---------- Trae ----------


def _trae_pack(limit: float, used: float, expire: int | None) -> dict:
    pack: dict = {
        "entitlement_base_info": {"quota": {"credits_limit": limit}},
        "usage": {"credits_amount": used},
    }
    if expire is not None:
        pack["expire_time"] = expire
    return pack


def test_parse_trae_packs_sums_remaining():
    packs = [
        _trae_pack(100, 30, NOW + 86400),
        _trae_pack(50, 0, NOW + 10 * 86400),
    ]
    credits, expire = parse_trae_packs(packs, NOW)
    assert credits == 120.0
    # 最早到期 = 明天
    assert expire == NOW + 86400


def test_parse_trae_packs_excludes_exhausted_from_expiry():
    """用完的包不能参与「最早到期」，否则账号会被伪装成快过期。"""
    packs = [
        _trae_pack(100, 100, NOW + 3600),   # 已用完，1 小时后到期
        _trae_pack(50, 0, NOW + 30 * 86400),  # 还有余额，30 天后
    ]
    credits, expire = parse_trae_packs(packs, NOW)
    assert credits == 50.0
    assert expire == NOW + 30 * 86400


def test_parse_trae_packs_ignores_unbounded_packs():
    packs = [{"entitlement_base_info": {"quota": {}}, "usage": {"credits_amount": 5}}]
    credits, expire = parse_trae_packs(packs, NOW)
    assert credits == 0.0
    assert expire is None


def test_parse_trae_packs_clamps_negative_remaining():
    packs = [_trae_pack(10, 99, None)]
    credits, _ = parse_trae_packs(packs, NOW)
    assert credits == 0.0


def test_parse_trae_packages_extracts_subscription_and_rows():
    packs = [
        {
            "display_desc": "会员 Pro 连续包月",
            "group_name": "会员积分",
            "group_type": 0,
            "expire_time": NOW + 86400,
            "entitlement_base_info": {"quota": {"credits_limit": 4000}},
            "usage": {"credits_amount": 4000},
        },
        {
            "display_desc": "老用户福利",
            "group_name": "用户福利",
            "group_type": 4,
            "expire_time": NOW + 86400,
            "entitlement_base_info": {"quota": {"credits_limit": 2000}},
            "usage": {"credits_amount": 500},
        },
    ]
    packages, subscription = parse_trae_packages(packs, NOW)
    assert subscription == "会员 Pro 连续包月"
    assert len(packages) == 2
    assert packages[0].remaining == 0.0
    assert packages[1].remaining == 1500.0
    assert packages[1].kind == "用户福利"


# ---------- WorkBuddy ----------


CN = timezone(timedelta(hours=8))


def _cn_epoch(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=CN).timestamp())


def test_parse_wb_resources_sums_and_picks_earliest():
    items = [
        {"CycleCapacityRemainPrecise": 120.5, "DeductionEndTime": "2026-11-01 00:00:00"},
        {"CycleCapacityRemainPrecise": 30, "DeductionEndTime": "2026-10-25 00:00:00"},
    ]
    credits, expire = parse_wb_resources(items, NOW)
    assert credits == 150.5
    # 最早到期 = 2026-10-25 00:00 +08:00
    assert expire == _cn_epoch(2026, 10, 25)


def test_parse_wb_resources_derives_remaining_from_total_minus_used():
    items = [{"CycleCapacitySizePrecise": 200, "CycleCapacityUsedPrecise": 50}]
    credits, _ = parse_wb_resources(items, NOW)
    assert credits == 150.0


def test_parse_wb_resources_excludes_exhausted_expiry():
    items = [
        {"CycleCapacityRemainPrecise": 0, "DeductionEndTime": "2026-10-20 00:00:00"},
        {"CycleCapacityRemainPrecise": 10, "DeductionEndTime": "2026-11-15 00:00:00"},
    ]
    _, expire = parse_wb_resources(items, NOW)
    # 不能是 10-20（那个包已用完）
    assert expire == _cn_epoch(2026, 11, 15)


# ---------- 凭据可查性 ----------


def test_trae_oauth_token_credentials():
    jwt, device_id, host = trae_credentials(
        {"format": "oauth-token", "accessToken": "j.w.t", "deviceId": "d1", "host": "https://api.trae.cn"}
    )
    assert jwt == "j.w.t"
    assert device_id == "d1"
    assert host == "https://api.trae.cn"


def test_trae_oauth_token_without_device_id_derives_one():
    """扫码凭据没有设备号时必须补一个 —— 空串会让签到固定吃 9004。"""
    jwt, device_id, _ = trae_credentials(
        {"format": "oauth-token", "accessToken": "j.w.t", "uid": "1001"}
    )
    assert jwt == "j.w.t"
    assert device_id and device_id.isdigit() and len(device_id) == 16

    # 同一账号派生出的设备号必须稳定（重启/刷新令牌都不能变），否则会重复签到。
    _, again, _ = trae_credentials(
        {"format": "oauth-token", "accessToken": "另一个令牌", "uid": "1001"}
    )
    assert again == device_id

    # 不同账号必须不同，否则会互相挤掉当天的设备名额（9095）。
    _, other, _ = trae_credentials(
        {"format": "oauth-token", "accessToken": "j.w.t", "uid": "1002"}
    )
    assert other != device_id


def test_workbuddy_raw_file_with_envelope_raises():
    """加密信封必须诚实报「不可查询」，不能显示 0。"""
    payload = json.dumps({"auth": {"accessToken": {"$wbEncrypted": 1, "envelope": "xx"}}}).encode()
    credential = {
        "format": "raw-file",
        "encoding": "base64",
        "data": base64.b64encode(payload).decode(),
    }
    with pytest.raises(CreditUnavailable) as exc:
        workbuddy_credentials(credential)
    assert "加密信封" in str(exc.value)


def test_workbuddy_raw_file_with_plaintext_token():
    payload = json.dumps(
        {"auth": {"accessToken": "plain", "domain": "www.workbuddy.cn"}, "account": {"uid": "u1"}}
    ).encode()
    credential = {
        "format": "raw-file",
        "encoding": "base64",
        "data": base64.b64encode(payload).decode(),
    }
    token, uid, domain = workbuddy_credentials(credential)
    assert token == "plain"
    assert uid == "u1"
    assert domain == "www.workbuddy.cn"


# ---------- 服务缓存 ----------


class _FakeRepo:
    def __init__(self, credential: dict) -> None:
        self._credential = credential
        self.calls = 0

    def current_credential(self, account_id: str) -> dict:
        self.calls += 1
        return self._credential


class _FakeAccount:
    id = "a1"
    scope = "workbuddy-cn"


def test_service_caches_within_ttl(monkeypatch):
    repo = _FakeRepo({"format": "oauth-token", "accessToken": "t"})
    service = CreditsService(repo, ttl=60)

    calls = {"n": 0}

    def _fake(client, credential, now_ts):
        calls["n"] += 1
        return CreditInfo(available=True, credits=42.0)

    monkeypatch.setattr("chaierww.domain.credits.fetch_workbuddy_credits", _fake)
    first = service.query_many([_FakeAccount()])
    second = service.query_many([_FakeAccount()])
    assert first["a1"].credits == 42.0
    assert second["a1"].credits == 42.0
    # 第二次命中缓存，没有再查
    assert calls["n"] == 1

    third = service.query_many([_FakeAccount()], force=True)
    assert third["a1"].credits == 42.0
    assert calls["n"] == 2
    service.close()


# ---------- 端点选择（令牌域对齐） ----------


def test_workbuddy_origin_always_api_domain():
    """API 接口（计费/签到/成长/用量）统一发 codebuddy.cn，不再按 token 域选。

    实测（2026-10-19）：扫码 token 即便 domain 是 www.workbuddy.cn，发往
    codebuddy.cn 同样 200；发往 workbuddy.cn 反而对非 Web 客户端 403。
    所以 origin 恒为官方 API 域，绝不回显账号数据（防主机注入）。
    """
    assert workbuddy_origin("www.workbuddy.cn") == "https://www.codebuddy.cn"
    assert workbuddy_origin("workbuddy.cn") == "https://www.codebuddy.cn"
    assert workbuddy_origin("  WWW.WorkBuddy.CN  ") == "https://www.codebuddy.cn"
    assert workbuddy_origin("www.codebuddy.cn") == "https://www.codebuddy.cn"
    # 账号里可能存的是完整 URL
    assert workbuddy_origin("https://www.workbuddy.cn/profile") == "https://www.codebuddy.cn"
    assert workbuddy_origin("https://www.codebuddy.cn/x?y=1") == "https://www.codebuddy.cn"
    # 空域 / 未知域 / 外站域：只能是官方常量，绝不回显账号数据
    assert workbuddy_origin("") == "https://www.codebuddy.cn"
    assert workbuddy_origin("evil.example.com") == "https://www.codebuddy.cn"
    assert "evil" not in workbuddy_origin("evil.example.com")

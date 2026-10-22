from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from chaierww.config import Settings
from chaierww.main import create_app

SCOPE = "workbuddy-cn"


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(home=tmp_path))
    with TestClient(app) as test_client:
        yield test_client


def _seed(client: TestClient, external_id="u-1", name="账号甲", token="tok-secret"):
    return client.app.state.repository.upsert(
        scope=SCOPE,
        external_id=external_id,
        display_name=name,
        credential={"access_token": token},
    )


def _seed_switching(client: TestClient, external_id="u-1", name="账号甲"):
    """用 raw-file 格式的凭据，供切换/预演相关测试走完 decode_payload。"""
    from chaierww.domain.switching import encode_payload

    return client.app.state.repository.upsert(
        scope=SCOPE,
        external_id=external_id,
        display_name=name,
        credential=encode_payload(b'{"login": "fake"}'),
    )


def test_health_reports_secret_backend(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["secrets"]
    # 侧栏要显示版本号，这个字段不能凭空消失。
    assert body["version"]


def test_scopes_marks_only_implemented_ones(client):
    scopes = client.get("/api/scopes").json()["scopes"]
    assert len(scopes) == 6
    implemented = {s["key"] for s in scopes if s["implemented"]}
    assert implemented == {"workbuddy-cn", "trae-cn"}


def test_list_returns_empty_for_new_scope(client):
    response = client.get("/api/accounts", params={"scope": SCOPE})
    assert response.status_code == 200
    assert response.json() == {"accounts": []}


def test_unknown_scope_is_400(client):
    response = client.get("/api/accounts", params={"scope": "nope"})
    assert response.status_code == 400


def test_missing_scope_is_rejected(client):
    # 参数缺失交给框架做请求校验（422）；语义错误的取值由领域层判（400）。
    response = client.get("/api/accounts")
    assert response.status_code == 422


def test_unimplemented_scope_is_501(client):
    response = client.get("/api/accounts", params={"scope": "qoder-cn"})
    assert response.status_code == 501


def test_created_account_is_visible(client):
    _seed(client)
    accounts = client.get("/api/accounts", params={"scope": SCOPE}).json()["accounts"]
    assert len(accounts) == 1
    assert accounts[0]["displayName"] == "账号甲"
    assert accounts[0]["credentialState"] == "unverified"


def test_response_never_contains_credential_material(client):
    _seed(client, token="tok-super-secret")
    raw = client.get("/api/accounts", params={"scope": SCOPE}).text
    assert "tok-super-secret" not in raw
    assert "access_token" not in raw


def test_stats_reflects_accounts(client):
    _seed(client, external_id="u-1")
    _seed(client, external_id="u-2", name="账号乙")
    body = client.get("/api/accounts/stats", params={"scope": SCOPE}).json()
    assert body["total"] == 2


def test_delete_then_recycle_bin_then_restore(client):
    account_id = _seed(client).account.id

    assert client.delete(f"/api/accounts/{account_id}").status_code == 200
    assert client.get("/api/accounts", params={"scope": SCOPE}).json()["accounts"] == []

    recycle = client.get("/api/accounts/recycle-bin", params={"scope": SCOPE}).json()["accounts"]
    assert [a["id"] for a in recycle] == [account_id]

    assert client.post(f"/api/accounts/{account_id}/restore").status_code == 200
    assert len(client.get("/api/accounts", params={"scope": SCOPE}).json()["accounts"]) == 1


def test_recycle_bin_without_scope_covers_merged_pool(client):
    """号池页是合并号池，前端拿不到单一平台，所以不能传 scope。

    这个参数以前是必填的 —— 结果是「保留 30 天可恢复」这句承诺在界面上根本无法兑现。
    """
    account_id = _seed(client).account.id
    client.delete(f"/api/accounts/{account_id}")

    body = client.get("/api/accounts/recycle-bin").json()
    assert [item["id"] for item in body["accounts"]] == [account_id]
    entry = body["accounts"][0]
    assert entry["daysLeft"] == 30  # 刚删的号满额保留，前端不用自己算日期
    assert entry["platform"]  # 回收站列表要能显示平台徽标
    assert entry["deletedAt"]


def test_restore_brings_back_the_credential(client):
    """软删除只打标记，凭据还在 —— 恢复后直接切号，不用重新扫码。"""
    account_id = _seed(client).account.id
    client.delete(f"/api/accounts/{account_id}")
    assert client.post(f"/api/accounts/{account_id}/restore").status_code == 200

    credential = client.app.state.repository.current_credential(account_id)
    assert credential["access_token"] == "tok-secret"


def test_delete_missing_account_is_404(client):
    assert client.delete("/api/accounts/does-not-exist").status_code == 404


def test_note_can_be_updated(client):
    account_id = _seed(client).account.id
    response = client.patch(f"/api/accounts/{account_id}/note", json={"note": "备用号"})
    assert response.status_code == 200
    assert response.json()["note"] == "备用号"


def test_merged_lists_accounts_with_platform(client):
    _seed(client, external_id="wb-1", name="WorkBuddy 号")
    _seed(client, external_id="wb-2", name="第二个 WorkBuddy 号")

    body = client.get("/api/accounts/merged").json()
    accounts = body["accounts"]
    assert len(accounts) == 2
    assert {a["platform"] for a in accounts} == {"workbuddy"}
    assert all(a["scope"] == SCOPE for a in accounts)


def test_switch_scope_can_be_omitted(client):
    """合并号池下前端不传 scope，接口从 accountId 反查平台。"""
    account = _seed_switching(client).account
    response = client.post(
        "/api/switch/dry-run", json={"accountId": account.id}
    )
    # 预演会走到「客户端是否运行」的判断，但 scope 反查必须成功，不应 400/404。
    assert response.status_code in (200, 409)
    assert response.json()["scope"] == SCOPE


def test_switch_unknown_account_is_404_without_scope(client):
    response = client.post("/api/switch/dry-run", json={"accountId": "no-such-id"})
    assert response.status_code == 404


def test_switch_corrupt_credential_is_400_not_500(client):
    """凭据损坏应返回 400（可纠正），而不是吞成 500。"""
    account = _seed(client).account
    response = client.post("/api/switch/dry-run", json={"accountId": account.id})
    assert response.status_code == 400
    assert "凭据" in response.json()["detail"]


def test_import_without_scope_auto_detects_logged_in_client(client, monkeypatch):
    """统一号池：点「导入」不传平台，后端探测已登录客户端。"""
    from chaierww.adapters.base import Detection, ImportedAccount

    adapter = client.app.state.adapters["workbuddy-cn"]

    monkeypatch.setattr(
        adapter,
        "detect",
        lambda: Detection(
            installed=True, running=False, logged_in=True,
            state_dir=None, app_path=None, current_external_id="u-logged-in",
        ),
    )
    monkeypatch.setattr(
        adapter,
        "import_current",
        lambda: ImportedAccount(
            external_id="u-logged-in", display_name="已登录的号", payload=b'{"login": 1}',
        ),
    )

    response = client.post("/api/accounts/import", json={})
    assert response.status_code == 200
    assert response.json()["account"]["externalId"] == "u-logged-in"
    assert response.json()["account"]["platform"] == "workbuddy"


def test_import_without_scope_and_nobody_logged_in_is_409(client, monkeypatch):
    from chaierww.adapters.base import Detection

    for adapter in client.app.state.adapters.values():
        monkeypatch.setattr(
            adapter,
            "detect",
            lambda: Detection(
                installed=True, running=False, logged_in=False,
                state_dir=None, app_path=None, current_external_id=None,
            ),
        )

    response = client.post("/api/accounts/import", json={})
    assert response.status_code == 409
    assert "登录" in response.json()["detail"]


def test_detection_lists_implemented_clients_with_download_urls(client, monkeypatch):
    from chaierww.adapters.base import Detection

    for adapter in client.app.state.adapters.values():
        monkeypatch.setattr(
            adapter,
            "detect",
            lambda: Detection(
                installed=False, running=False, logged_in=False,
                state_dir=None, app_path=None, current_external_id=None,
            ),
        )

    body = client.get("/api/detection").json()
    clients = body["clients"]
    # 只有已实现的作用域才会出现在合并检测里
    scopes = {c["scope"] for c in clients}
    assert scopes == {"workbuddy-cn", "trae-cn"}
    # 每个客户端都带引导下载链接（含邀请码）
    for item in clients:
        assert item["downloadUrl"]
        assert item["installed"] is False


def test_import_json_auto_detects_workbuddy(client):
    content = json.dumps(
        {"account": {"uid": "u-json-1", "uin": "330000"}, "auth": {}},
        ensure_ascii=False,
    )
    response = client.post("/api/accounts/import-json", json={"content": content})
    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True
    assert body["account"]["scope"] == "workbuddy-cn"
    assert body["account"]["externalId"] == "u-json-1"


def test_import_json_auto_detects_trae(client):
    content = json.dumps(
        {"iCubeAuthInfo://icube-dc:1111222233334444": "dGMFEAAA" + "x" * 100},
        ensure_ascii=False,
    )
    response = client.post("/api/accounts/import-json", json={"content": content})
    assert response.status_code == 200
    body = response.json()
    assert body["account"]["scope"] == "trae-cn"
    assert body["account"]["externalId"] == "1111222233334444"


def test_import_json_unrecognized_is_400(client):
    response = client.post("/api/accounts/import-json", json={"content": "{\"foo\": 1}"})
    assert response.status_code == 400
    assert "识别" in response.json()["detail"]


def test_import_json_reimport_updates_not_creates(client):
    content = json.dumps({"account": {"uid": "u-json-1", "uin": "330000"}, "auth": {}})
    first = client.post("/api/accounts/import-json", json={"content": content})
    assert first.json()["created"] is True
    second = client.post("/api/accounts/import-json", json={"content": content})
    assert second.json()["created"] is False


def test_preferences_defaults_and_roundtrip(client):
    body = client.get("/api/preferences").json()
    # 保活 / 自动签到已收敛到计划任务，常规设置里只剩「切换迁移」与两个自动备份开关。
    assert body == {
        "migrateMemory": True,
        "migrateConnectors": True,
        "migrateSessions": True,
        "migrateProfile": True,
        "migrateWorkspaces": True,
        # 网页版登录态（按账号隔离的 Chromium 分区）随切号迁移，默认开。
        "migrateWebview": True,
        "autoBackup": True,
        # 全局备份的每日自动备份：默认开，开关在设置 → 备份页。
        "autoGlobalBackup": True,
        # 自动备份收录的分区：默认只有不含凭据的 config。
        "autoGlobalParts": ["config"],
    }

    # 局部更新：不传的字段保持原值
    again = client.put(
        "/api/preferences",
        json={"migrateSessions": False, "migrateProfile": False},
    )
    assert again.json() == {
        "migrateMemory": True,
        "migrateConnectors": True,
        "migrateSessions": False,
        "migrateProfile": False,
        "migrateWorkspaces": True,
        "migrateWebview": True,
        "autoBackup": True,
        "autoGlobalBackup": True,
        "autoGlobalParts": ["config"],
    }

    # 自动备份开关能单独关掉，且不影响切换迁移那组
    off = client.put("/api/preferences", json={"autoGlobalBackup": False})
    assert off.json()["autoGlobalBackup"] is False
    assert off.json()["migrateSessions"] is False
    assert off.json()["autoBackup"] is True

    # 持久化：重新读取一致
    assert client.get("/api/preferences").json() == off.json()


def test_preferences_auto_global_parts_cleaned(client):
    """自动备份分区：只认白名单内的 key，未知的被丢弃，空列表回落 config。"""
    # 勾选含凭据分区（gateway / accounts）后持久化
    saved = client.put(
        "/api/preferences",
        json={"autoGlobalParts": ["config", "gateway", "accounts"]},
    )
    assert saved.json()["autoGlobalParts"] == ["config", "gateway", "accounts"]

    # 掺入未知 key + 重复：去重、丢未知
    cleaned = client.put(
        "/api/preferences",
        json={"autoGlobalParts": ["accounts", "evil", "accounts", "gateway"]},
    )
    assert cleaned.json()["autoGlobalParts"] == ["accounts", "gateway"]

    # 空列表回落默认（不含凭据的 config）
    empty = client.put("/api/preferences", json={"autoGlobalParts": []})
    assert empty.json()["autoGlobalParts"] == ["config"]


def test_migration_preview_is_read_only(client, tmp_path, monkeypatch):
    """GET /api/migration/preview 只读推演，不写任何文件 / 库 / 台账。"""
    import chaierww.domain.migration as migration_mod
    import chaierww.domain.profile_migration as profile_mod
    import chaierww.domain.session_migration as session_migration_mod
    import chaierww.domain.sessions as sessions_mod

    data = tmp_path / "wb"
    data.mkdir()
    for module in (migration_mod, profile_mod, session_migration_mod, sessions_mod):
        monkeypatch.setattr(module, "workbuddy_data_dir", lambda: data)

    account = _seed_switching(client).account
    response = client.get(
        "/api/migration/preview",
        params={"accountId": account.id, "sourceUid": "u-src"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["targetUid"] == "u-1"
    expected = {"memory", "connectors", "sessions", "profile", "workspaces", "duplicates"}
    assert expected <= set(body)
    # 预检不得落任何文件（无备份目录、无台账）
    assert not (data / "backups").exists()
    assert not (data / "buddy-switch-copy-ledger.json").exists()




def test_oauth_start_returns_verification_uri(client, monkeypatch):
    monkeypatch.setattr(
        client.app.state.oauth,
        "start",
        lambda: {
            "loginId": "wb_test",
            "verificationUri": "https://www.workbuddy.cn/login?state=xyz",
            "expiresIn": 600,
        },
    )
    body = client.post("/api/oauth/start").json()
    assert body["loginId"] == "wb_test"
    assert "login?state=" in body["verificationUri"]


def test_oauth_status_saves_token_account(client, monkeypatch):
    monkeypatch.setattr(
        client.app.state.oauth,
        "poll",
        lambda login_id: {
            "done": True,
            "result": {
                "uid": "oauth-uid-1",
                "nickname": "扫码用户",
                "accessToken": "plain-token",
                "refreshToken": "refresh-token",
                "domain": "www.workbuddy.cn",
                "auth_raw": {},
                "profile_raw": {},
            },
        },
    )
    body = client.get("/api/oauth/status", params={"loginId": "wb_x"}).json()
    assert body["done"] is True
    assert body["result"]["account"]["externalId"] == "oauth-uid-1"
    assert body["result"]["account"]["scope"] == "workbuddy-cn"
    # 凭据是 oauth-token 形态
    from chaierww.domain.switching import decode_payload

    account = client.app.state.repository.get(body["result"]["account"]["id"])
    cred = client.app.state.repository.current_credential(account.id)
    decoded = decode_payload(cred)
    assert decoded.format == "oauth-token"
    assert decoded.account["accessToken"] == "plain-token"


def test_oauth_status_not_done_passthrough(client, monkeypatch):
    monkeypatch.setattr(client.app.state.oauth, "poll", lambda login_id: {"done": False})
    body = client.get("/api/oauth/status", params={"loginId": "wb_y"}).json()
    assert body["done"] is False
    assert "result" not in body


def test_trae_oauth_start_reads_storage(client, monkeypatch):
    adapter = client.app.state.adapters["trae-cn"]
    monkeypatch.setattr(adapter, "read_storage_json", lambda: {"iCubeAuthInfo://icube-dc:x": "b64"})
    monkeypatch.setattr(
        client.app.state.trae_oauth,
        "start",
        lambda storage: {
            "loginId": "trae_test",
            "verificationUri": "https://www.trae.cn/authorization?x=1",
            "expiresIn": 300,
            "port": 17388,
        },
    )
    body = client.post("/api/trae/oauth/start").json()
    assert body["loginId"] == "trae_test"
    assert body["verificationUri"].startswith("https://www.trae.cn/authorization")
    assert body["port"] == 17388


def test_trae_oauth_start_missing_storage(client, monkeypatch):
    adapter = client.app.state.adapters["trae-cn"]
    from chaierww.adapters.base import NotLoggedIn

    def _raise():
        raise NotLoggedIn("未找到登录态文件")

    monkeypatch.setattr(adapter, "read_storage_json", _raise)
    resp = client.post("/api/trae/oauth/start")
    assert resp.status_code == 409


def test_trae_oauth_status_saves_token_account(client, monkeypatch):
    monkeypatch.setattr(
        client.app.state.trae_oauth,
        "poll",
        lambda login_id: {
            "done": True,
            "result": {
                "uid": "1111222233334444",
                "displayName": "Trae 用户",
                "accessToken": "jwt.token.sig",
                "refreshToken": "refresh-token",
                "host": "https://api.trae.cn",
                "deviceId": "1111222233334444",
            },
        },
    )
    body = client.get("/api/trae/oauth/status", params={"loginId": "trae_x"}).json()
    assert body["done"] is True
    assert body["result"]["account"]["externalId"] == "1111222233334444"
    assert body["result"]["account"]["scope"] == "trae-cn"

    from chaierww.domain.switching import decode_payload

    account = client.app.state.repository.get(body["result"]["account"]["id"])
    cred = client.app.state.repository.current_credential(account.id)
    decoded = decode_payload(cred)
    assert decoded.format == "oauth-token"
    assert decoded.account["accessToken"] == "jwt.token.sig"


def test_trae_oauth_cancel(client, monkeypatch):
    monkeypatch.setattr(client.app.state.trae_oauth, "cancel", lambda login_id: True)
    body = client.post("/api/trae/oauth/cancel", params={"loginId": "trae_z"}).json()
    assert body["ok"] is True


def test_account_credits_returns_per_account_map(client, monkeypatch):
    from chaierww.domain.credits import CreditInfo

    seeded = _seed_switching(client, external_id="cred-1", name="积分账号")
    monkeypatch.setattr(
        client.app.state.credits,
        "query_many",
        lambda accounts, force=False: {
            seeded.account.id: CreditInfo(
                available=True, credits=88.5, expires_at=1_800_000_000, expiring_soon=True
            )
        },
    )
    body = client.get("/api/accounts/credits").json()
    item = body["credits"][seeded.account.id]
    assert item["available"] is True
    assert item["credits"] == 88.5
    assert item["expiresAt"] == 1_800_000_000
    assert item["expiringSoon"] is True


def test_account_credits_marks_unavailable_honestly(client, monkeypatch):
    from chaierww.domain.credits import CreditInfo

    seeded = _seed_switching(client, external_id="cred-2", name="信封账号")
    monkeypatch.setattr(
        client.app.state.credits,
        "query_many",
        lambda accounts, force=False: {
            seeded.account.id: CreditInfo(available=False, error="凭据是加密信封")
        },
    )
    body = client.get("/api/accounts/credits").json()
    item = body["credits"][seeded.account.id]
    # 不可查 ≠ 0 积分
    assert item["available"] is False
    assert item["credits"] is None




def test_api_usage_returns_items_and_summary(client, monkeypatch):
    from chaierww.domain.usage import AccountUsage

    seeded = _seed(client, external_id="usage-1", name="用量账号")
    monkeypatch.setattr(
        client.app.state.usage,
        "query_many",
        lambda accounts, force=False: [
            AccountUsage(
                account_id=seeded.account.id,
                display_name="用量账号",
                platform="workbuddy",
                scope="workbuddy-cn",
                detectable=True,
                requests=5,
                credit_consumed=12.5,
            )
        ],
    )
    body = client.get("/api/usage").json()
    assert body["summary"]["totalRequests"] == 5
    account = body["accounts"][0]
    assert account["detectable"] is True
    assert account["requests"] == 5


def test_api_usage_request_turn_returns_dialogue(client, monkeypatch):
    from chaierww.domain.usage_turns import UsageTurn

    seeded = _seed(client, external_id="usage-1", name="用量账号")
    monkeypatch.setattr(
        "chaierww.api.routes.conversations_for_user",
        lambda uid: {"cid-1": ("聊过的会话", "D:/proj")},
    )
    monkeypatch.setattr(
        "chaierww.api.routes.read_turn",
        lambda request_id, allowed=None: UsageTurn(
            request_id=request_id,
            conversation_id="cid-1",
            messages=(
                {"role": "user", "text": "这个页面改一下", "at": 1, "model": None},
                {"role": "assistant", "text": "改好了", "at": 2, "model": "hy4"},
            ),
            available=True,
        ),
    )
    body = client.get(
        "/api/usage/requests/r-1", params={"accountId": seeded.account.id}
    ).json()
    assert body["found"] is True
    assert body["conversationId"] == "cid-1"
    assert body["title"] == "聊过的会话"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]


def test_api_usage_request_turn_rejects_other_account(client, monkeypatch):
    from chaierww.domain.usage_turns import UsageTurn

    seeded = _seed(client, external_id="usage-1", name="用量账号")
    monkeypatch.setattr(
        "chaierww.api.routes.conversations_for_user", lambda uid: {"cid-1": ("标题", "/w")}
    )
    monkeypatch.setattr(
        "chaierww.api.routes.read_turn",
        lambda request_id, allowed=None: UsageTurn(
            request_id=request_id,
            conversation_id="cid-other",
            messages=(),
            available=True,
        ),
    )
    body = client.get(
        "/api/usage/requests/r-1", params={"accountId": seeded.account.id}
    ).json()
    assert body["found"] is False
    assert body["messages"] == []
    assert "不属于" in body["error"]


def test_api_conversation_messages_checks_ownership(client, monkeypatch):
    """会话不属于该账号时必须 404，避免凭会话 id 越权读别人的聊天记录。"""
    seeded = _seed(client, external_id="sess-1", name="会话账号")

    monkeypatch.setattr(
        "chaierww.api.routes.conversation_owner", lambda cid: "someone-else"
    )
    response = client.get(f"/api/sessions/{seeded.account.id}/cid-1/messages")
    assert response.status_code == 404
    assert "不属于" in response.json()["detail"]


def test_api_conversation_messages_returns_transcript(client, monkeypatch):
    seeded = _seed(client, external_id="sess-1", name="会话账号")

    monkeypatch.setattr(
        "chaierww.api.routes.conversation_owner", lambda cid: "sess-1"
    )
    monkeypatch.setattr(
        "chaierww.api.routes.read_conversation_messages",
        lambda cid: [
            {"role": "user", "text": "你好", "at": 1000, "model": None},
            {"role": "assistant", "text": "你好呀", "at": 2000, "model": "glm-5"},
        ],
    )
    body = client.get(f"/api/sessions/{seeded.account.id}/cid-1/messages").json()
    assert [item["role"] for item in body["messages"]] == ["user", "assistant"]
    assert body["messages"][1]["text"] == "你好呀"
    assert body["conversationId"] == "cid-1"


def test_schedules_lists_all_tasks(client):
    """五个任务：保活 / 签到 / 全部活动 / 派猫猫派出 / 派猫猫回领。"""
    tasks = client.get("/api/schedules").json()["tasks"]
    assert [item["key"] for item in tasks] == [
        "keepalive",
        "checkin",
        "activity_run",
        "travel_depart",
        "travel_claim",
    ]
    assert {"enabled", "times", "jitterMinutes", "accountIds", "lastRunAt", "nextRunAt"} <= set(
        tasks[0]
    )
    # 派猫猫两个阶段默认关，且默认时点是「上午派、下午领」
    by_key = {item["key"]: item for item in tasks}
    assert by_key["travel_depart"]["enabled"] is False
    assert by_key["travel_depart"]["times"] == ["09:30"]
    assert by_key["travel_claim"]["times"] == ["15:30"]


def test_schedules_update_times_and_jitter(client):
    body = client.put(
        "/api/schedules/keepalive",
        json={"times": ["07:30", "19:00"], "jitterMinutes": 5, "accountIds": ["a-1"]},
    ).json()["task"]
    assert body["times"] == ["07:30", "19:00"]
    assert body["jitterMinutes"] == 5
    assert body["accountIds"] == ["a-1"]

    # 前一次改动已落盘，再读一次一致。
    again = client.get("/api/schedules").json()["tasks"][0]
    assert again["times"] == ["07:30", "19:00"]


def test_schedules_update_rejects_bad_times(client):
    response = client.put("/api/schedules/keepalive", json={"times": ["25:00"]})
    assert response.status_code == 400


def test_schedules_are_the_only_owner_of_keepalive_and_checkin(client):
    """保活 / 签到只归计划任务管；常规设置里不再有第二套镜像开关。

    默认值：保活与签到默认开启（原先由常规设置的偏好给出同样的默认值），
    全部活动是重动作，默认关。
    """

    def tasks() -> dict[str, dict]:
        return {item["key"]: item for item in client.get("/api/schedules").json()["tasks"]}

    initial = tasks()
    assert initial["keepalive"]["enabled"] is True
    assert initial["checkin"]["enabled"] is True
    assert initial["activity_run"]["enabled"] is False

    client.put("/api/schedules/checkin", json={"enabled": False})
    assert tasks()["checkin"]["enabled"] is False
    # 常规设置里不该再冒出 autoCheckin 这个键。
    assert "autoCheckin" not in client.get("/api/preferences").json()

    # 反过来也不成立：改常规设置不会影响计划任务（多给的老字段直接被忽略）。
    client.put(
        "/api/preferences",
        json={"migrateSessions": False, "keepalive": True, "autoCheckin": True},
    )
    assert tasks()["checkin"]["enabled"] is False
    assert tasks()["keepalive"]["enabled"] is True


def test_schedule_run_now_reports_result(client):
    body = client.post("/api/schedules/keepalive/run-now").json()
    assert body["key"] == "keepalive"
    assert body["lastRunAt"]
    assert body["result"]
    # 空号池不应触发任何上游请求，只如实回报「跳过」。
    assert "没有可用账号" in body["result"]
    assert body["task"]["lastResult"] == body["result"]


# ---------- 概览待处理清单 ----------


def _seed_id(client: TestClient, state=None) -> str:
    """建一个账号并（可选）把它设成给定凭据状态，返回 account id。"""
    from chaierww.domain.accounts.models import CredentialState

    _seed(client)
    account_id = client.get("/api/accounts", params={"scope": SCOPE}).json()["accounts"][0]["id"]
    if state is not None:
        assert isinstance(state, CredentialState)
        client.app.state.repository.set_credential_state(account_id, state)
    return account_id


def _stub_overview(client, monkeypatch, *, detected, credits, clients) -> None:
    """三类数据源全部打桩：这一屏绝不能真去访问上游。"""
    monkeypatch.setattr(
        client.app.state.activities, "detect", lambda accounts, force=False: list(detected)
    )
    monkeypatch.setattr(
        client.app.state.credits, "query_many", lambda accounts, force=False: dict(credits)
    )
    monkeypatch.setattr("chaierww.api.routes._detection_items", lambda request: list(clients))


def _pending_activity(account_id: str, *, done: bool = False):
    from chaierww.domain.activities import STATE_DONE, STATE_TODO, AccountActivities, ActivityItem

    return AccountActivities(
        account_id=account_id,
        display_name="账号甲",
        platform="workbuddy",
        scope=SCOPE,
        detectable=True,
        items=(
            ActivityItem(
                key="workbuddy.daily_checkin",
                platform="workbuddy",
                group="daily",
                title="每日签到",
                state=STATE_DONE if done else STATE_TODO,
                automatable=True,
            ),
        ),
    )


def _client_row(*, installed: bool = True, running: bool = True) -> dict:
    return {
        "scope": SCOPE,
        "platform": "workbuddy",
        "label": "WorkBuddy 国内版",
        "installed": installed,
        "running": running,
        "loggedIn": True,
        "stateDir": None,
        "appPath": None,
        "message": "",
        "currentAccountId": None,
        "downloadUrl": None,
    }


def test_overview_actionables_splits_four_sources(client, monkeypatch):
    from chaierww.domain.accounts.models import CredentialState
    from chaierww.domain.credits import CreditInfo

    account_id = _seed_id(client, CredentialState.RELOGIN_REQUIRED)
    _stub_overview(
        client,
        monkeypatch,
        detected=[_pending_activity(account_id)],
        credits={account_id: CreditInfo(available=True, credits=88.0, expiring_soon=True)},
        clients=[_client_row(installed=True, running=False)],
    )

    body = client.get("/api/overview/actionables").json()
    assert set(body) == {"items", "pulse"}
    by_kind = {item["kind"]: item for item in body["items"]}
    assert set(by_kind) == {"credential", "checkin", "credits_expiring", "client_offline"}

    assert by_kind["credential"]["severity"] == "high"
    assert by_kind["credential"]["accountId"] == account_id
    assert by_kind["credential"]["action"] == {"type": "navigate", "to": "/accounts"}

    assert by_kind["checkin"]["accountId"] == account_id
    assert by_kind["checkin"]["action"] == {
        "type": "run",
        "endpoint": "/activities/run",
        "accountIds": [account_id],
    }

    assert by_kind["credits_expiring"]["detail"].startswith("账号甲")
    assert by_kind["credits_expiring"]["action"] == {"type": "navigate", "to": "/usage"}

    # 客户端按平台报一次，不带 accountId。
    assert by_kind["client_offline"]["accountId"] is None

    # 高优先级排最前。
    assert body["items"][0]["kind"] == "credential"


def test_overview_actionables_empty_when_all_clear(client, monkeypatch):
    from chaierww.domain.accounts.models import CredentialState
    from chaierww.domain.credits import CreditInfo

    account_id = _seed_id(client, CredentialState.USABLE)
    _stub_overview(
        client,
        monkeypatch,
        detected=[_pending_activity(account_id, done=True)],
        credits={account_id: CreditInfo(available=True, credits=10.0, expiring_soon=False)},
        clients=[_client_row(installed=True, running=True)],
    )

    body = client.get("/api/overview/actionables").json()
    assert body["items"] == []
    # 速览是只读汇总：活动全部完成、积分正常、客户端在线、网关无流量。
    assert body["pulse"]["activityActionable"] == 0
    assert body["pulse"]["activityDone"] == 1
    assert body["pulse"]["creditsExpiring"] == 0
    assert body["pulse"]["clientsRunning"] == 1
    assert body["pulse"]["gateway"]["requests"] == 0


def test_overview_actionables_is_read_only(client, monkeypatch):
    from chaierww.domain.accounts.models import CredentialState
    from chaierww.domain.credits import CreditInfo

    account_id = _seed_id(client, CredentialState.RELOGIN_REQUIRED)
    _stub_overview(
        client,
        monkeypatch,
        detected=[_pending_activity(account_id)],
        credits={account_id: CreditInfo(available=True, credits=88.0, expiring_soon=True)},
        clients=[_client_row(installed=True, running=False)],
    )

    history_before = client.get("/api/switch/history").json()
    backups_before = client.get("/api/backups").json()
    accounts_before = client.get("/api/accounts/merged").json()["accounts"]

    assert client.get("/api/overview/actionables").json()["items"]

    # 清单是只读聚合：不切号、不备份、不改账号。
    assert client.get("/api/switch/history").json() == history_before
    assert client.get("/api/backups").json() == backups_before
    assert client.get("/api/accounts/merged").json()["accounts"] == accounts_before


def test_restore_snapshot_overwrites_state_and_keeps_safety_backup(
    client, tmp_path, monkeypatch
):
    """恢复：先校验、再给当前状态落一份「恢复前」快照，最后才覆盖客户端目录。"""
    import shutil

    adapter = client.app.state.adapters[SCOPE]
    state = tmp_path / "client-state"
    state.mkdir()
    (state / "login.json").write_text('{"uid": "u-1"}', encoding="utf-8")

    monkeypatch.setattr(adapter, "state_dir", lambda: state)
    monkeypatch.setattr(adapter, "is_running", lambda: False)

    created = client.post("/api/backups", json={"scope": SCOPE, "note": "手动备份"}).json()
    assert created["fileCount"] == 1

    # 备份之后把当前状态改脏：恢复应把它还原成快照里的内容。
    (state / "login.json").write_text('{"uid": "u-2"}', encoding="utf-8")

    def _restore_from(source_dir):
        for item in source_dir.iterdir():
            if item.is_file():
                shutil.copy2(item, state / item.name)

    monkeypatch.setattr(adapter, "restore_from", _restore_from)

    response = client.post(f"/api/backups/{created['id']}/restore")
    assert response.status_code == 200
    assert (state / "login.json").read_text(encoding="utf-8") == '{"uid": "u-1"}'

    safety_id = response.json()["safetySnapshotId"]
    assert safety_id
    # 恢复成功后，pre-restore 安全快照立即删除，不堆积常驻快照；
    # 列表里只剩最初那份手动备份。
    listed = client.get("/api/backups", params={"scope": SCOPE}).json()["snapshots"]
    ids = {item["id"] for item in listed}
    assert safety_id not in ids
    assert created["id"] in ids


def test_restore_rejects_corrupted_snapshot(client, tmp_path, monkeypatch):
    """校验不过的快照不允许覆盖：损坏的备份比没有备份更危险。"""
    adapter = client.app.state.adapters[SCOPE]
    state = tmp_path / "client-state"
    state.mkdir()
    (state / "login.json").write_text('{"uid": "u-1"}', encoding="utf-8")

    monkeypatch.setattr(adapter, "state_dir", lambda: state)
    monkeypatch.setattr(adapter, "is_running", lambda: False)
    monkeypatch.setattr(adapter, "restore_from", lambda _source: None)

    created = client.post("/api/backups", json={"scope": SCOPE}).json()
    snapshot = client.app.state.backups.get(created["id"])
    (snapshot.directory / "login.json").write_text('{"uid": "tampered"}', encoding="utf-8")

    response = client.post(f"/api/backups/{created['id']}/restore")
    assert response.status_code == 500
    assert "校验" in response.json()["detail"]
    # 校验失败时一个字节都不该动。
    assert (state / "login.json").read_text(encoding="utf-8") == '{"uid": "u-1"}'


def test_export_account_bundle_contains_credentials(client):
    """导出物必须带解密后的凭据：没有凭据的包导回去也切不了号。"""
    import json

    seeded = _seed(client, external_id="u-1", name="账号甲", token="tok-a")
    client.app.state.repository.update_note(seeded.account.id, "主号")

    response = client.post("/api/accounts/export", json={"accountIds": [seeded.account.id]})
    assert response.status_code == 200
    bundle = response.json()
    assert bundle["format"] == "chaierww.accounts"
    assert bundle["version"] == 1
    assert bundle["skipped"] == 0
    entry = bundle["accounts"][0]
    assert entry["externalId"] == "u-1"
    assert entry["displayName"] == "账号甲"
    assert entry["note"] == "主号"
    assert entry["credentialKind"] == "session"
    assert entry["credential"] == {"access_token": "tok-a"}
    # 凭据是明文，能重新封成合法 JSON 文本（导入侧直接读它）。
    assert json.loads(json.dumps(entry["credential"]))["access_token"] == "tok-a"


def test_export_requires_explicit_selection(client):
    _seed(client)
    response = client.post("/api/accounts/export", json={"accountIds": []})
    assert response.status_code == 400
    assert "选择" in response.json()["detail"]


def test_import_bundle_restores_account_and_note(client):
    """导出 → 删除 → 导回：账号回到号池、备注保留、凭据状态回到未验证。"""
    import json

    seeded = _seed(client, external_id="u-1", name="账号甲", token="tok-a")
    client.app.state.repository.update_note(seeded.account.id, "主号")
    bundle = client.post(
        "/api/accounts/export", json={"accountIds": [seeded.account.id]}
    ).json()

    client.delete(f"/api/accounts/{seeded.account.id}")

    response = client.post(
        "/api/accounts/import-bundle", json={"content": json.dumps(bundle)}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["imported"] == 1
    assert body["restored"] == 1
    assert body["failed"] == 0

    account = client.app.state.repository.get(seeded.account.id)
    assert account.is_deleted is False
    assert account.note == "主号"
    assert account.credential_state.value == "unverified"
    assert account.source == "json_bundle"
    kind, credential = client.app.state.repository.credential_record(account.id)
    assert kind == "session"
    assert credential == {"access_token": "tok-a"}


def test_import_bundle_skips_bad_entries_but_keeps_good_ones(client):
    """一个坏条目只废掉它自己，不让整包都导不进去。"""
    import json

    good = _seed(client, external_id="u-1", name="账号甲", token="tok-a")
    bundle = client.post("/api/accounts/export", json={"accountIds": [good.account.id]}).json()
    bundle["accounts"].append(
        {
            "scope": SCOPE,
            "externalId": "u-9",
            "displayName": "",
            "credentialKind": "session",
            "credential": {"access_token": "tok-x"},
        }
    )

    response = client.post(
        "/api/accounts/import-bundle", json={"content": json.dumps(bundle)}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["imported"] == 1
    assert body["failed"] == 1
    assert "第 2 条" in body["errors"][0]


def test_import_bundle_rejects_foreign_json(client):
    response = client.post(
        "/api/accounts/import-bundle",
        json={"content": '{"foo": "bar"}'},
    )
    assert response.status_code == 400
    assert "本工具导出" in response.json()["detail"]


def test_export_flags_missing_refresh_token(client):
    """导出包里有凭据 ≠ 导回去能长期用：缺 refresh token 的号必须被标出来。"""
    seeded = _seed(client, external_id="u-1", name="账号甲", token="tok-a")

    bundle = client.post("/api/accounts/export", json={"accountIds": [seeded.account.id]}).json()

    assert bundle["accounts"][0]["refreshTokenMissing"] is True


def test_export_does_not_flag_complete_credential(client):
    from chaierww.domain.switching import encode_token_payload

    seeded = client.app.state.repository.upsert(
        scope=SCOPE,
        external_id="u-2",
        display_name="完整号",
        credential=encode_token_payload(
            {"uid": "u-2", "accessToken": "a", "refreshToken": "b"}
        ),
        credential_kind="oauth_token",
    )

    bundle = client.post("/api/accounts/export", json={"accountIds": [seeded.account.id]}).json()

    assert bundle["accounts"][0]["refreshTokenMissing"] is False


def test_credential_health_lists_only_accounts_with_gaps(client):
    from chaierww.domain.switching import encode_token_payload

    _seed(client, external_id="u-1", name="缺 refresh")
    client.app.state.repository.upsert(
        scope=SCOPE,
        external_id="u-2",
        display_name="完整号",
        credential=encode_token_payload(
            {"uid": "u-2", "accessToken": "a", "refreshToken": "b"}
        ),
        credential_kind="oauth_token",
    )

    body = client.get("/api/accounts/credential-health").json()

    assert body["withGaps"] == 1
    assert [item["displayName"] for item in body["accounts"]] == ["缺 refresh"]
    assert body["accounts"][0]["gaps"] == ["refreshToken"]
    # 体检接口只回答「缺什么」，不能顺手把凭据内容带出来。
    assert "credential" not in body["accounts"][0]

"""客户端设置托管（阶段 5.1）单测。

重点不是「能不能改 JSON」，而是「除了目标值那一段，其它字节一个都不能变」。
所以最小补丁的用例一律用**逐字节相等的整份文本**断言，而不是只比解析结果。

夹具按本机 `~/.workbuddy/settings.json` 的**真实形状**造：BOM + CRLF + 4 空格缩进、
嵌套对象、数组、以及那三个一次性迁移标记 —— 它们都必须原样留下。
全程只碰临时目录，不碰真实客户端文件。
"""

from __future__ import annotations

import pytest

from chaierww.domain.client_settings import (
    BACKUP_KEEP,
    SPEC_BY_KEY,
    ClientSettingsError,
    ClientSettingsService,
    SettingSpec,
    fingerprint,
    patch_minimal,
    read_values,
)

#: 与真实文件同形：BOM + CRLF + 4 空格缩进 + 嵌套对象 / 数组 + 一次性迁移标记。
RAW = (
    "\ufeff{\r\n"
    "    \"autoLaunchDefaultApplied\": true,\r\n"
    "    \"autoLaunchDesired\": false,\r\n"
    "    \"claw\": {\r\n"
    "        \"channels\": [],\r\n"
    "        \"legacyOwnerUid\": \"u-123\",\r\n"
    "        \"users\": {}\r\n"
    "    },\r\n"
    "    \"enabledPlugins\": {\r\n"
    "        \"workbuddy-docx@workbuddy-builtin\": true\r\n"
    "    },\r\n"
    "    \"officeFileAssociationsRepairMarker\": \"我自己加的\",\r\n"
    "    \"pluginConfigs\": [],\r\n"
    "    \"sandbox\": {\r\n"
    "        \"extraAllowWrite\": [],\r\n"
    "        \"networkDenyAllMigrated\": true,\r\n"
    "        \"orderedRules\": []\r\n"
    "    }\r\n"
    "}\r\n"
)

#: 只有布尔键名不同（用来验证「插键」路径）：把目标键整行删掉后的文本。
WITHOUT_TARGET = RAW.replace("    \"autoLaunchDesired\": false,\r\n", "")


def write_raw(path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def read_raw(path) -> str:
    """用 read_bytes 读回：`read_text` 会把 CRLF 归一成 LF，掩盖字节级差异。"""
    return path.read_bytes().decode("utf-8")


def test_patch_minimal_preserves_every_other_byte():
    """只翻 autoLaunchDesired 的值；其余（BOM / CRLF / 缩进 / 嵌套 / 标记）逐字节不变。"""
    patched = patch_minimal(RAW, SPEC_BY_KEY["autoLaunchDesired"], True)

    assert patched == RAW.replace(
        "\"autoLaunchDesired\": false", "\"autoLaunchDesired\": true"
    )
    assert patched.startswith("\ufeff")
    assert "\r\n" in patched
    # 一次性迁移标记与嵌套结构原样都在。
    assert "\"autoLaunchDefaultApplied\": true" in patched
    assert "\"officeFileAssociationsRepairMarker\": \"我自己加的\"" in patched
    assert "\"legacyOwnerUid\": \"u-123\"" in patched
    assert "\"enabledPlugins\":" in patched
    assert "\"workbuddy-docx@workbuddy-builtin\": true" in patched


def test_patch_minimal_handles_nested_string_spec():
    """字符串键 / 点分嵌套键走同一套最小补丁：只换字符串字面量。"""
    spec = SettingSpec(key="claw.legacyOwnerUid", label="示例", kind="string", default="", hint="")
    patched = patch_minimal(RAW, spec, "u-456")
    assert patched == RAW.replace(
        "\"legacyOwnerUid\": \"u-123\"", "\"legacyOwnerUid\": \"u-456\""
    )


def test_patch_minimal_refuses_to_create_nested_key():
    """嵌套键不存在时不自动创建（要建中间对象就得重排结构，不算最小补丁）。"""
    spec = SettingSpec(key="sandbox.notThere", label="示例", kind="bool", default=False, hint="")
    with pytest.raises(ClientSettingsError, match="嵌套键不会被自动创建"):
        patch_minimal(RAW, spec, True)


def test_patch_minimal_inserts_absent_key_only():
    """键不存在时最小插入：只在最后一个成员后加一段，别的字节不动。"""
    inserted = patch_minimal(WITHOUT_TARGET, SPEC_BY_KEY["autoLaunchDesired"], True)

    expected = WITHOUT_TARGET.replace(
        "        \"orderedRules\": []\r\n    }\r\n",
        "        \"orderedRules\": []\r\n    },\r\n    \"autoLaunchDesired\": true\r\n",
    )
    assert inserted == expected


def test_patch_minimal_inserts_into_empty_object():
    spec = SPEC_BY_KEY["autoLaunchDesired"]
    assert patch_minimal("{}", spec, True) == '{\n  "autoLaunchDesired": true\n}'
    assert patch_minimal("{\n}", spec, True) == '{\n  "autoLaunchDesired": true\n}'


def test_patch_minimal_tolerates_trailing_comma():
    raw = "{\n  \"autoLaunchDefaultApplied\": true,\n}"
    patched = patch_minimal(raw, SPEC_BY_KEY["autoLaunchDesired"], True)
    # 已有尾逗号，就不再补一个。
    assert patched == "{\n  \"autoLaunchDefaultApplied\": true,\n  \"autoLaunchDesired\": true\n}"


def test_fingerprint_ignores_values_but_tracks_format():
    same_shape = RAW.replace("\"autoLaunchDesired\": false", "\"autoLaunchDesired\": true")
    assert fingerprint(RAW) == fingerprint(same_shape)

    reindented = RAW.replace("    ", "  ")
    assert fingerprint(reindented) != fingerprint(RAW)

    reordered = RAW.replace(
        "    \"autoLaunchDefaultApplied\": true,\r\n    \"autoLaunchDesired\": false,\r\n",
        "    \"autoLaunchDesired\": false,\r\n    \"autoLaunchDefaultApplied\": true,\r\n",
    )
    assert fingerprint(reordered) != fingerprint(RAW)


def test_read_values_reports_presence_and_value():
    values = read_values(RAW)
    assert values["autoLaunchDesired"] == (True, False)

    values = read_values(WITHOUT_TARGET)
    assert values["autoLaunchDesired"] == (False, None)


# ---------- 服务层 ----------


@pytest.fixture()
def service(tmp_path):
    settings_file = tmp_path / ".workbuddy" / "settings.json"
    return ClientSettingsService(settings_file, tmp_path / "backups")


def seed(service) -> None:
    service._file.parent.mkdir(parents=True, exist_ok=True)
    write_raw(service._file, RAW)


def test_snapshot_when_file_missing(service):
    snapshot = service.snapshot()
    assert snapshot["available"] is False
    assert snapshot["fingerprint"] is None
    assert all(not item["present"] for item in snapshot["specs"])
    assert snapshot["backups"] == []


def test_write_rejects_unregistered_key(service):
    seed(service)
    with pytest.raises(ClientSettingsError, match="未登记"):
        service.write({"legacyOwnerUid": "u-999"}, fingerprint(RAW))
    # 拒绝写之后文件必须原样。
    assert read_raw(service._file) == RAW


def test_write_rejects_stale_fingerprint(service):
    seed(service)
    with pytest.raises(ClientSettingsError, match="格式"):
        service.write({"autoLaunchDesired": True}, "stale-fingerprint")
    assert read_raw(service._file) == RAW


def test_write_validates_type(service):
    seed(service)
    current = fingerprint(RAW)
    with pytest.raises(ClientSettingsError, match="true / false"):
        service.write({"autoLaunchDesired": "yes"}, current)
    with pytest.raises(ClientSettingsError, match="true / false"):
        service.write({"autoLaunchDesired": 1}, current)


def test_write_patches_and_backs_up(service):
    seed(service)

    result = service.write({"autoLaunchDesired": True}, fingerprint(RAW))

    assert result["changed"] == [
        {
            "key": "autoLaunchDesired",
            "label": "客户端开机自启",
            "before": False,
            "after": True,
        }
    ]
    assert result["backupId"]
    assert read_raw(service._file) == RAW.replace(
        "\"autoLaunchDesired\": false", "\"autoLaunchDesired\": true"
    )
    # 指纹只改值时不变，前端可连续写入。
    assert result["fingerprint"] == fingerprint(RAW)
    assert [item.id for item in service.backups()] == [result["backupId"]]


def test_write_same_value_is_noop(service):
    seed(service)
    result = service.write({"autoLaunchDesired": False}, fingerprint(RAW))
    assert result["changed"] == []
    assert result["backupId"] is None
    assert "没有变化" in result["settings"]["message"]
    assert service.backups() == []


def test_restore_puts_backup_content_back(service):
    seed(service)

    first = service.write({"autoLaunchDesired": True}, fingerprint(RAW))
    service.write({"autoLaunchDesired": False}, first["fingerprint"])

    restored = service.restore(first["backupId"])
    assert restored["available"] is True
    # 逐字节回到第一次写入前的样子（含 CRLF 与 BOM）。
    assert read_raw(service._file) == RAW
    # 恢复前会把当前内容再备份一次，所以备份点不会只剩一个。
    assert len(service.backups()) >= 2


def test_restore_rejects_unknown_backup(service):
    with pytest.raises(ClientSettingsError, match="不存在"):
        service.restore("settings-nope")


def test_backups_are_pruned(service):
    seed(service)

    confirm = fingerprint(RAW)
    for _ in range(BACKUP_KEEP + 3):
        # 来回翻同一项：每次都真的写盘一次，于是每次都落一份备份。
        spec_value = not service.snapshot()["specs"][0]["value"]
        confirm = service.write({"autoLaunchDesired": spec_value}, confirm)["fingerprint"]

    assert len(service.backups()) == BACKUP_KEEP

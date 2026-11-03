"""诊断包：日志尾部出包前必须再过一遍值级脱敏。"""

from __future__ import annotations

import io
import json
import zipfile

from chaierww.config import Settings
from chaierww.domain.diagnostics import build_health, build_zip
from chaierww.infra.startup import write_startup_error
from chaierww.main import create_app

CHECKS = [
    "database",
    "config",
    "data_dir",
    "logs_dir",
    "disk",
    "scheduler",
    "gateway",
    "startup",
]

OLD_LOG = "\n".join(
    [
        "2026-10-20 09:00:00 INFO    chaierww 数据目录 C:\\Users\\19951\\.chaierww",
        "2026-10-20 09:00:01 INFO    chaierww 登录成功：someone@example.com",
        "2026-10-20 09:00:02 ERROR   chaierww 上游失败 token=abcdef123456",
        "",
    ]
)


def test_health_checks_cover_eight_items(tmp_path):
    app = create_app(Settings(home=tmp_path))
    health = build_health(app)

    assert [item["id"] for item in health["checks"]] == CHECKS
    assert all(item["level"] in {"ok", "warn", "error"} for item in health["checks"])
    kinds = {item["id"]: item for item in health["checks"]}
    assert kinds["database"]["level"] == "ok"
    assert kinds["config"]["level"] == "ok"


def test_health_reports_unwritable_dirs(tmp_path, monkeypatch):
    app = create_app(Settings(home=tmp_path))
    monkeypatch.setattr("chaierww.domain.diagnostics._writable", lambda _path: False)

    kinds = {item["id"]: item for item in build_health(app)["checks"]}
    assert kinds["data_dir"]["level"] == "error"
    assert kinds["logs_dir"]["level"] == "error"


def test_health_flags_startup_error(tmp_path):
    app = create_app(Settings(home=tmp_path))
    write_startup_error(tmp_path, RuntimeError("boom"))

    kinds = {item["id"]: item for item in build_health(app)["checks"]}
    assert kinds["startup"]["level"] == "warn"
    assert kinds["startup"]["value"] == "RuntimeError"


def test_log_tail_is_redacted_in_package(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "chaierww.log").write_text(OLD_LOG, encoding="utf-8")

    app = create_app(Settings(home=tmp_path))
    with zipfile.ZipFile(io.BytesIO(build_zip(app))) as archive:
        log_names = [name for name in archive.namelist() if name.startswith("logs/")]
        assert log_names
        body = archive.read(log_names[0]).decode("utf-8")

        assert "someone@example.com" not in body
        assert "s***@example.com" in body
        assert "abcdef123456" not in body
        assert "19951" not in body

        report = json.loads(archive.read("report.json").decode("utf-8"))
        dumped = json.dumps(report, ensure_ascii=False)
        assert "sealed" not in dumped
        assert "envelope" not in dumped

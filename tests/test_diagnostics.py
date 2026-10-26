"""诊断包：日志尾部出包前必须再过一遍值级脱敏。"""

from __future__ import annotations

import io
import json
import zipfile

from chaierww.config import Settings
from chaierww.domain.diagnostics import build_zip
from chaierww.main import create_app

OLD_LOG = "\n".join(
    [
        "2026-10-20 09:00:00 INFO    chaierww 数据目录 C:\\Users\\19951\\.chaierww",
        "2026-10-20 09:00:01 INFO    chaierww 登录成功：someone@example.com",
        "2026-10-20 09:00:02 ERROR   chaierww 上游失败 token=abcdef123456",
        "",
    ]
)


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

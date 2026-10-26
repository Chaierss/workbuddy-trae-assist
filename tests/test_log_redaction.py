"""日志值级脱敏：写盘前把凭据与个人标识打码，覆盖所有日志调用点。"""

from __future__ import annotations

import logging

import pytest

from chaierww.logging_setup import (
    LOGGER_NAME,
    mask_identity,
    redact_text,
    setup_logging,
)

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmn"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"Authorization: Bearer {JWT}", "Authorization: Bearer ***"),
        ("token=abcdef123456", "token=***"),
        ('"access_token": "abcdef123456"', '"access_token": "***"'),
        ("日志里留下 someone@example.com 的邮箱", "日志里留下 s***@example.com 的邮箱"),
        ("联系 13800138000", "联系 138****8000"),
        (r"数据目录 C:\Users\19951\.chaierww", r"数据目录 C:\Users\***\.chaierww"),
        (f"sk-{'a' * 32}", "sk-***"),
        (f"裸串 {JWT} 不该保留", "裸串 *** 不该保留"),
        ("2026-10-26 INFO chaierww 启动完成", "2026-10-26 INFO chaierww 启动完成"),
    ],
)
def test_redact_text(raw: str, expected: str) -> None:
    assert redact_text(raw) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("someone@example.com", "s***@example.com"),
        ("13800138000", "138****8000"),
        ("小明", "小明"),
        ("Chaier", "Chaier"),
    ],
)
def test_mask_identity(name: str, expected: str) -> None:
    assert mask_identity(name) == expected


def test_filter_writes_masked_line_to_file(tmp_path) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    setup_logging(tmp_path / "logs")
    logger.info("登录成功：%s，手机号 %s", "someone@example.com", "13800138000")
    for handler in list(logger.handlers):
        handler.flush()
        handler.close()
        logger.removeHandler(handler)

    body = (tmp_path / "logs" / "chaierww.log").read_text(encoding="utf-8")
    assert "someone@example.com" not in body
    assert "s***@example.com" in body
    assert "13800138000" not in body
    assert "138****8000" in body

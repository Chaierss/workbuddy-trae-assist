from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOGGER_NAME = "chaierww"
_SENSITIVE_MARKERS = ("token", "secret", "password", "credential", "authorization", "cookie")


def redact(value: object) -> str:
    """日志脱敏：只保留长度与前 4 位，绝不输出完整凭据。"""
    text = str(value)
    if not text:
        return "<empty>"
    head = text[:4]
    return f"{head}…(len={len(text)})"


def looks_sensitive(field_name: str) -> bool:
    lowered = field_name.lower()
    return any(marker in lowered for marker in _SENSITIVE_MARKERS)


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger

    logs_dir.mkdir(parents=True, exist_ok=True)
    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = TimedRotatingFileHandler(
        logs_dir / "chaierww.log", when="midnight", backupCount=14, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    return logger

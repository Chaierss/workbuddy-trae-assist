from __future__ import annotations

import logging
import re
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOGGER_NAME = "chaierww"
_SENSITIVE_MARKERS = ("token", "secret", "password", "credential", "authorization", "cookie")

# 值级脱敏规则：按顺序执行，先打「键=值」再打独立出现的长串，
# 避免刚打好的 *** 被后面的规则当成新值再处理。
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization: Bearer eyJhbGciOi...
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-+/=]{8,}"), r"\1 ***"),
    # token=abc / "access_token": "abc" / password: abc
    # `(?!bearer)` 是必须的：否则上面刚打好的「Bearer ***」会被当成
    # authorization 的值再打一遍，结果变成「*** ***」，排查时看不出原貌。
    (
        re.compile(
            r"(?i)\b(password|passwd|token|secret|api[_\-]?key|access[_\-]?token"
            r"|refresh[_\-]?token|credential|authorization)\b([\"']?\s*[=:]\s*)"
            r"(?!bearer\b)([\"']?)([^\s\"',}]{4,})"
        ),
        r"\1\2\3***",
    ),
    # 形如 {"$wbEncrypted":1,"envelope":"...."} 的整段密文
    (re.compile(r"(?i)(\"envelope\"\s*:\s*\")[^\"]{8,}(\")"), r"\1***\2"),
    # 邮箱：保留首字符与域名，既能对上号又不可反查
    (
        re.compile(
            r"(?<![\w.+-])([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![\w-])"
        ),
        r"\1***@\2",
    ),
    # 中国大陆手机号：138****1234
    (re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)"), r"\1****\2"),
    # 本机家目录：日志里带上用户名没有排查价值，只会跟着诊断包外发
    (re.compile(r"(?i)\b([A-Z]:[\\/]+Users[\\/]+)[^\\/\s\"']+"), r"\1***"),
    # 常见令牌形态：OpenAI / GitHub / Slack / AWS
    (re.compile(r"\bsk-[A-Za-z0-9]{24,}"), "sk-***"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "ghp_***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "github_pat_***"),
    (re.compile(r"\bxox[bp]-[A-Za-z0-9-]{10,}"), "xox***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***"),
    # JWT 三段式
    (re.compile(r"\beyJ[\w-]{10,}\.eyJ[\w-]{10,}\.[\w-]{10,}"), "***"),
)

_EMAIL_FULL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_FULL = re.compile(r"1[3-9]\d{9}")


def redact_text(text: str) -> str:
    """把一段文本里可能存在的凭据与个人标识打码，保留结构便于排查。"""
    result = text
    for pattern, replacement in _REDACTIONS:
        result = pattern.sub(replacement, result)
    return result


def mask_identity(name: str) -> str:
    """账号显示名脱敏：邮箱/手机号打码，普通昵称原样保留（昵称不是凭据）。"""
    stripped = name.strip()
    if _EMAIL_FULL.fullmatch(stripped):
        local, _, domain = stripped.partition("@")
        return f"{local[:1]}***@{domain}"
    if _PHONE_FULL.fullmatch(stripped):
        return f"{stripped[:3]}****{stripped[-4:]}"
    return name


class RedactingFilter(logging.Filter):
    """在写盘/上屏前对整条消息做值级脱敏，新增日志调用点无需逐处处理。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # 格式化失败时交给原样输出，不能因为脱敏把日志丢了
            return True
        record.msg = redact_text(message)
        record.args = ()
        return True


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
    file_handler.addFilter(RedactingFilter())
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(RedactingFilter())
    logger.addHandler(console)
    return logger

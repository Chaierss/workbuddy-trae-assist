"""守住「版本库里不能出现凭证与运行时数据」这条线。

背景：`scripts/publish_snapshot.sh` 早就有一条敏感值扫描，但只在「走那个脚本发布」
时生效；日常 `git push` 完全不经过它。这个脚本把同一套规则拔出来，同时给 CI
和本地 pre-push 用，避免「备份、日志、数据库、内部文档」被顺手提交。

用法：
    python scripts/check_secrets.py            # 扫已入库文件
    python scripts/check_secrets.py --self-test  # 只验证规则本身

退出码：0 干净；1 有命中。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 二进制/生成物：要么不可读，要么本身就是几百 KB 的第三方产物。
SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".icns",
    ".bmp",
    ".exe",
    ".dll",
    ".pdf",
    ".zip",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".mp4",
    ".webm",
}
# 锁文件与自带的 Swagger 资源体积大、且必然出现大量随机串，扫了只会制造噪音。
SKIP_FILES = {"desktop/package-lock.json", "web/package-lock.json"}
SKIP_PREFIXES = ("src/chaierww/webassets/",)

MAX_BYTES = 2 * 1024 * 1024
MAX_HITS_PER_FILE = 5

# 路径闸门：这些东西一旦入库，等于把本机运行数据公开。
PATH_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(^|/)\.env(\.|$)"), "环境变量文件"),
    (re.compile(r"(^|/)(api-token|accounts\.json)$"), "运行时凭据文件"),
    (re.compile(r"\.db(-wal|-shm)?$"), "数据库文件"),
    (re.compile(r"(^|/)logs/|\.log$"), "日志文件"),
    (re.compile(r"(^|/)backups/"), "备份文件"),
    (re.compile(r"^desktop/release/|^desktop/backend/(dist|build)/"), "构建产物"),
    (re.compile(r"(^|/)_shot-"), "未脱敏原始截图"),
)

# 值闸门：只认「放进去就一定出事」的形态，避免误报把规则关掉。
VALUE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("jwt", re.compile(r"\beyJ[\w-]{10,}\.eyJ[\w-]{10,}\.[\w-]{10,}")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{24,}")),
    ("github-token", re.compile(r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[bp]-[A-Za-z0-9-]{10,}")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |PGP )?PRIVATE KEY-----")),
    ("phone-email", re.compile(r"[0-9]{11}@(?:163|qq|gmail|outlook|foxmail)\.")),
    ("user-path", re.compile(r"(?i)\b[A-Z]:\\+Users\\+\d{3,}")),
)

# 这些文件本身就是在「讲规则」，或者用于演示/测试，命中属预期。
FILE_ALLOW = {
    "SECURITY.md",
    "CHANGELOG.md",
    "scripts/check_secrets.py",
    "scripts/publish_snapshot.sh",
}
RULE_ALLOW: dict[str, set[str]] = {
    # 假密钥/假公钥夹具：协议解析测试需要固定形态的串。
    "tests/test_trae_oauth.py": {"private-key", "jwt"},
    "tests/test_trae_adapter.py": {"jwt"},
    "tests/test_trae_crypto.py": {"jwt"},
    # 脱敏用例本身就要拿「该被打码的样本」当输入，命中的是测试数据。
    "tests/test_log_redaction.py": {"jwt", "user-path"},
    "tests/test_diagnostics.py": {"user-path"},
}


def tracked(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        text=False,
        check=False,
    )
    return [item for item in result.stdout.decode("utf-8", "replace").split("\0") if item]


def path_violations(paths: list[str]) -> list[str]:
    hits: list[str] = []
    for path in paths:
        for pattern, label in PATH_RULES:
            if pattern.search(path):
                hits.append(f"{path}: 路径命中（{label}）")
                break
    return hits


def value_violations(root: Path, paths: list[str]) -> list[str]:
    hits: list[str] = []
    for relative in paths:
        if relative in SKIP_FILES or relative.startswith(SKIP_PREFIXES):
            continue
        path = root / relative
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        allowed = RULE_ALLOW.get(relative, set())
        if relative in FILE_ALLOW:
            continue
        found: list[str] = []
        for number, line in enumerate(text.splitlines(), start=1):
            for rule, pattern in VALUE_RULES:
                if rule in allowed or not pattern.search(line):
                    continue
                found.append(f"{relative}:{number}: 值命中（{rule}）")
                if len(found) >= MAX_HITS_PER_FILE:
                    break
            if len(found) >= MAX_HITS_PER_FILE:
                break
        hits.extend(found)
    return hits


def self_test() -> int:
    samples: tuple[tuple[str, str | None], ...] = (
        (
            "Authorization: Bearer "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmn",
            "jwt",
        ),
        (f"key=sk-{'a' * 32}", "openai-key"),
        (f"token=ghp_{'x' * 36}", "github-token"),
        ("AKIA1234567890ABCDEF", "aws-key"),
        ("xoxb-1234567890-abcdef", "slack-token"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----", "private-key"),
        ("13800138000@qq.com", "phone-email"),
        (r"C:\Users\19951\AppData\Local", "user-path"),
        ("2026-10-26 INFO chaierww 启动完成", None),
        ("网关端口 8788 正在监听", None),
    )
    failures: list[str] = []
    for line, expected in samples:
        matched = [rule for rule, pattern in VALUE_RULES if pattern.search(line)]
        if expected is None:
            if matched:
                failures.append(f"误报：{line!r} 命中 {matched}")
        elif expected not in matched:
            failures.append(f"漏报：{line!r} 期望 {expected}，实际 {matched}")
    if failures:
        print("✗ 自测失败：")
        for failure in failures:
            print(f"    {failure}")
        return 1
    print(f"✓ 自测通过：{len(samples)} 条样本按预期判定")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="扫描版本库中的凭证与运行时数据")
    parser.add_argument("--root", type=Path, default=ROOT, help="仓库根目录")
    parser.add_argument("--self-test", action="store_true", help="只验证扫描规则")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    root = args.root.resolve()
    paths = tracked(root)
    if not paths:
        print("✗ 读不到 git 文件清单（是否不在仓库里？）")
        return 1

    hits = path_violations(paths) + value_violations(root, paths)
    if hits:
        print("✗ 版本库里出现了不该入库的东西：")
        for hit in hits:
            print(f"    {hit}")
        print("\n  处理方式：git rm --cached <文件>（本地文件保留），"
              "确认是误报才加进 allowlist。")
        return 1

    print(f"✓ 扫描 {len(paths)} 个已入库文件：未发现凭证或运行时数据")
    return 0


if __name__ == "__main__":
    sys.exit(main())

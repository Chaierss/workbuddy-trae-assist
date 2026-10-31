"""守住「哪些文档能进公开仓库」这条线。

背景：`docs/` 同时是两样东西——应用内「设置 → Wiki」的源目录，和公开仓库的文档目录。
所以往里加文件是件很自然的事，而这些文件默认**不该公开**（规划、规格、闭源清单都在里面）。
`.gitignore` 已经把 `docs/**/*.md` 排除掉了，但 `git add -f`、换台机器、改 .gitignore
都可能绕过它。这个脚本按白名单再查一遍，让 CI 把线守住。

用法：
    python scripts/check_public_docs.py

退出码：0 合规；1 有内部文档混进了版本库。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 允许进入公开仓库的 docs/ 文档（都在 README 里链出，属对外材料）。
# `docs/README.md` 是应用内 Wiki 唯一的公开篇目：不放行的话干净 clone 上 Wiki 只剩一篇免责声明。
# 要再加一个公开文档：这里加一项，同时在 .gitignore 里加一条 ! 例外。
ALLOWED = {
    "docs/README.md",
    "docs/policies/DISCLAIMER.md",
    "docs/QUICKSTART.md",
    "docs/QUICKSTART.en.md",
    "docs/LINUX.md",
    "docs/policies/TELEMETRY.md",
}


def tracked(pathspec: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", pathspec],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def main() -> int:
    docs = tracked("docs")
    leaked = [p for p in docs if p.endswith(".md") and p not in ALLOWED]

    if leaked:
        print("✗ 这些内部文档进了版本库（docs/ 默认不公开）：")
        for path in leaked:
            print(f"    {path}")
        print("\n  逐个移出索引（文件保留在本地，应用内 Wiki 仍可读）：")
        for path in leaked:
            print(f"    git rm --cached {path}")
        print("\n  如果确实要公开某一份，把它加进本脚本的 ALLOWED，")
        print("  并在 .gitignore 里补一条 ! 例外，两处必须同时改。")
        return 1

    missing = [p for p in ALLOWED if p not in docs]
    if missing:
        print("✗ 白名单里的文档不见了（README 会链接它，缺了就是死链）：")
        for path in missing:
            print(f"    {path}")
        return 1

    print(f"✓ docs/ 只入库了 {len(ALLOWED)} 个白名单文档，内部文档未泄漏")
    return 0


if __name__ == "__main__":
    sys.exit(main())

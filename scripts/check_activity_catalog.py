"""校验官方活动目录（CI / 发版前手动跑）。

用法：
    python scripts/check_activity_catalog.py

退出码 0 = 通过；1 = 有字段、主题、图标或官方入口域名不合规。
不访问网络，也不读取用户数据，只做静态清单校验。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chaierww.domain.activities import (  # noqa: E402
    ACTIVITY_CATALOG,
    OFFICIAL_CATALOG_VERSION,
    validate_official_catalog,
)


def main() -> int:
    try:
        validate_official_catalog()
    except ValueError as exc:
        print(f"[FAIL] 官方活动目录校验未通过：{exc}", file=sys.stderr)
        return 1
    print(
        f"[OK] 官方活动目录 v{OFFICIAL_CATALOG_VERSION}，"
        f"共 {len(ACTIVITY_CATALOG)} 个活动"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""一次性导入老项目账号池（buddy-switch 导出的两份 JSON）。

用法（在项目 venv 里）：
    python scripts/import_legacy_accounts.py <workbuddy.json> <trae.json>

也可以走环境变量（不传参时读取）：
    CHAIRWW_WB_JSON / CHAIRWW_TRAE_JSON

- WorkBuddy：顶层 access_token（明文）+ uid + domain + nickname → oauth-token 凭据。
  明文 token 可直接查积分/活动；个别账号 auth_raw.auth.accessToken 是 DPAPI 信封，
  但顶层 access_token 仍是明文，所以全部可查。
- Trae：jwt（Cloud-IDE-JWT 明文）+ UserID + name → oauth-token 凭据。
  按 external_id 去重（已存在的跳过并说明）。

输入路径不写死在脚本里：这两份导出文件通常落在桌面且含真实凭据，
写死路径等于把本机用户名一起提交进仓库。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 项目根加入 sys.path，保证能 import chaierww
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chaierww.config import load_settings  # noqa: E402
from chaierww.domain.accounts.repository import AccountRepository  # noqa: E402
from chaierww.domain.switching import encode_token_payload  # noqa: E402
from chaierww.infra.db import open_database  # noqa: E402
from chaierww.infra.secrets import default_box  # noqa: E402


def _input(arg_index: int, env_name: str) -> Path:
    """取输入文件：优先命令行参数，其次环境变量；都没有就明确报错。"""
    raw = os.environ.get(env_name, "").strip()
    if not raw and len(sys.argv) > arg_index:
        raw = sys.argv[arg_index].strip()
    if not raw:
        raise SystemExit(
            f"缺少输入文件（第 {arg_index} 个参数或环境变量 {env_name}）。\n"
            f"用法：python {Path(sys.argv[0]).name} <workbuddy.json> <trae.json>"
        )
    return Path(raw).expanduser()


def _wb_account(raw: dict) -> dict | None:
    token = str(raw.get("access_token") or "").strip()
    if not token:
        return None
    uid = str(raw.get("uid") or "").strip()
    if not uid:
        return None
    domain = str(raw.get("domain") or "www.codebuddy.cn").strip()
    nickname = str(raw.get("nickname") or f"账号{uid[-6:]}").strip()
    return {
        "uid": uid,
        "display_name": nickname,
        "domain": domain,
        "credential": encode_token_payload(
            {"uid": uid, "accessToken": token, "domain": domain}
        ),
    }


def _trae_account(raw: dict) -> dict | None:
    jwt = str(raw.get("jwt") or "").strip()
    if not jwt:
        return None
    uid = str(raw.get("UserID") or "").strip()
    if not uid:
        return None
    name = str(raw.get("name") or f"账号{uid[-6:]}").strip()
    refresh = str(raw.get("refresh_token") or "").strip()
    return {
        "uid": uid,
        "display_name": name,
        "credential": encode_token_payload(
            {
                "uid": uid,
                "displayName": name,
                "accessToken": jwt,
                "refreshToken": refresh,
                "host": "https://api.trae.cn",
                "deviceId": "",
            }
        ),
    }


def main() -> int:
    settings = load_settings()
    conn = open_database(settings.db_path)
    box = default_box()
    repo = AccountRepository(conn, box)

    # 输入路径在 main 里取：放在模块顶层会让「导入本脚本」这件事本身也失败。
    wb_file = _input(1, "CHAIRWW_WB_JSON")
    trae_file = _input(2, "CHAIRWW_TRAE_JSON")

    wb = json.loads(wb_file.read_text(encoding="utf-8"))
    trae = json.loads(trae_file.read_text(encoding="utf-8"))

    imported = 0
    skipped = 0

    print(f"WorkBuddy 账号 {len(wb)} 个：")
    for raw in wb:
        parsed = _wb_account(raw)
        if parsed is None:
            print("  ✗ 缺 token/uid，跳过")
            skipped += 1
            continue
        existing = repo.get_by_external("workbuddy-cn", parsed["uid"])
        if existing is not None:
            # 已存在但凭据是信封（raw-file）→ 用明文 token 覆盖，让它可查积分/活动。
            if existing.source == "local_import":
                result = repo.upsert(
                    scope="workbuddy-cn",
                    external_id=parsed["uid"],
                    display_name=parsed["display_name"],
                    credential=parsed["credential"],
                    credential_kind="oauth_token",
                    source="legacy_import",
                )
                print(f"  ~ 覆盖信封凭据：{parsed['display_name']}（改为明文可查）")
                imported += 1
            else:
                print(f"  = 已存在：{parsed['display_name']}（跳过）")
                skipped += 1
            continue
        result = repo.upsert(
            scope="workbuddy-cn",
            external_id=parsed["uid"],
            display_name=parsed["display_name"],
            credential=parsed["credential"],
            credential_kind="oauth_token",
            source="legacy_import",
        )
        print(f"  + 导入：{parsed['display_name']}（{result.account.id[:8]}）")
        imported += 1

    print(f"Trae 账号 {len(trae)} 个：")
    for raw in trae:
        parsed = _trae_account(raw)
        if parsed is None:
            print("  ✗ 缺 jwt/uid，跳过")
            skipped += 1
            continue
        existing = repo.get_by_external("trae-cn", parsed["uid"])
        if existing is not None:
            print(f"  = 已存在：{parsed['display_name']}（跳过）")
            skipped += 1
            continue
        result = repo.upsert(
            scope="trae-cn",
            external_id=parsed["uid"],
            display_name=parsed["display_name"],
            credential=parsed["credential"],
            credential_kind="oauth_token",
            source="legacy_import",
        )
        print(f"  + 导入：{parsed['display_name']}（{result.account.id[:8]}）")
        imported += 1

    conn.close()
    print(f"\n完成：新导入 {imported} 个，跳过 {skipped} 个。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

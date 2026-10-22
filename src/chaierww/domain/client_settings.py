"""客户端设置托管（P2 / G7）：读改上游客户端的 `settings.json`。

## 为什么需要它

上游客户端把「默认模型 / 界面主题 / 自动更新」这类**设置**放在自己的
`settings.json` 里。要让本工具在不破坏该文件的前提下改这几项，必须比
「整体重写 JSON」更克制 —— 客户端的文件里有我们既不认识、也不该动的键，
而且它的缩进/换行/键序都可能被用户或其它工具改过。

## 三道关（对齐 D7）

1. **白名单规格表**：只登记我们敢改的键（`SPECS`）。未登记的键一律不碰，
   读回来的值也**从不**出现在响应里（避免把整个文件暴露给前端）。
2. **格式指纹**：`fingerprint(text)` 取「缩进风格 + 行尾 + BOM + 顶层键序」。
   写入前把指纹与前端带上来的比对，变了就拒绝写并要求用户刷新重来 ——
   这样「读」和「写」之间文件被别的东西改过时，我们不会误覆盖。
   指纹**不含值**，所以只改值时它不变，前端可以连续改多次而不必重读。
3. **字节级最小补丁**：`patch_minimal()` 只替换目标值所在的 token，
   其余字节（含 BOM、缩进、换行、注释外的一切空白）**逐字节原样保留**。
   键不存在时才走 `_insert_member`，且只在一处插入，不动其它字节。

写入前还会自动落一份备份点（滚动保留 `BACKUP_KEEP` 份），恢复也走本模块。

## 落位

- 备份放**我们自己的**数据目录（`~/.chaierww/client-settings/`），
  绝不在上游客户端目录里留下文件；往返只碰那一个 `settings.json`。
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

#: 客户端设置文件名（WorkBuddy / CodeBuddy CLI 数据目录下）。
FILENAME = "settings.json"

#: 覆盖设置文件位置的兜底环境变量（打包 / 测试 / 非默认安装盘用）。
SETTINGS_PATH_ENV = "CHAIERWW_WORKBUDDY_SETTINGS"

#: 备份滚动保留份数（与 `domain/backup.py` 的滚动语义一致）。
BACKUP_KEEP = 7

BACKUP_PREFIX = "settings-"

_INDENT_DEFAULT = "  "

_BOM_CHAR = "\ufeff"


class ClientSettingsError(RuntimeError):
    """客户端设置相关错误。消息面向用户，可直接展示。"""


# ---------- 规格表（白名单） ----------


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """一个「我们敢改」的客户端设置键。

    `key` 是**键路径**：`autoLaunchDesired` 是顶层键，`a.b` 是顶层 `a` 里的 `b`
    （但嵌套键不存在时不会自动创建，见 `_insert_member`）。

    `kind` 只支持 `string` / `bool`：这两种的 JSON 字面量可以**原地替换**
    （字符串 / true / false），不需要重排任何字符；对象与数组不在登记范围内。
    """

    key: str
    label: str
    kind: str
    default: Any
    hint: str

    def payload(self, *, present: bool, value: Any) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "default": self.default,
            "hint": self.hint,
            "present": present,
            "value": value,
        }


#: 登记表 —— 就是白名单本身。加键只改这里，且必须是 `string` / `bool`。
#:
#: 这份名单刻意很短：**只登记在真实文件里实测过、且改它不会触发客户端副作用**的键。
#: 本机实测 `~/.workbuddy/settings.json` 的顶层键是
#: `autoLaunchDesired` / `autoLaunchDefaultApplied` / `officeFileAssociationsRepairMarker` /
#: `officeMarkdownDefaultClaimAttempted` / `claw` / `enabledPlugins` / `pluginConfigs` /
#: `sandbox` —— 其中三个 `*Marker`/`*Applied`/`*Attempted` 是**一次性迁移标记**
#: （改了会让客户端重跑迁移），`claw` / `enabledPlugins` / `pluginConfigs` / `sandbox`
#: 是账号数据与运行期状态；真正能安全改的只有 `autoLaunchDesired`（开机自启意向）。
#: 没有实测过的键**一律不登记**：往用户的真实文件里插一个客户端不认识的键，
#: 比这个功能少几个开关糟得多。
SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(
        key="autoLaunchDesired",
        label="客户端开机自启",
        kind="bool",
        default=False,
        hint="客户端是否随系统启动。这是客户端自己 settings.json 里的意向值，"
        "改完以客户端实际行为为准。",
    ),
)

SPEC_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in SPECS}


def default_settings_file() -> Path:
    """客户端设置文件的**登记位置**。可用环境变量整体覆盖。

    与 `domain/sessions.py` 的 `workbuddy_data_dir()` 同源：WorkBuddy 的 CLI
    数据目录是 `~/.workbuddy`（记忆 / 连接器 / 会话库都在那），设置文件也在那。
    """
    override = os.environ.get(SETTINGS_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".workbuddy" / FILENAME


# ---------- 类型校验 ----------


def _coerce(spec: SettingSpec, raw: Any) -> Any:
    """把前端送来的值收敛到规格声明的类型；不合法一律拒绝（fail closed）。"""
    if spec.kind == "string":
        if not isinstance(raw, str):
            raise ClientSettingsError(f"「{spec.label}」需要一个字符串")
        return raw
    if spec.kind == "bool":
        # `bool` 是 `int` 的子类，但这里只收真正的布尔，不收 0/1。
        if not isinstance(raw, bool):
            raise ClientSettingsError(f"「{spec.label}」需要 true / false")
        return raw
    raise ClientSettingsError(f"「{spec.label}」的登记类型 {spec.kind} 不受支持")


# ---------- JSON 文本扫描（定位值 token，只读、不改文本） ----------


@dataclass(frozen=True, slots=True)
class _Member:
    key: str
    value_start: int
    value_end: int


@dataclass(frozen=True, slots=True)
class _Object:
    open_pos: int
    close_pos: int
    members: tuple[_Member, ...]


#: 扫描时跳过的空白。BOM 也算：它可能出现在文件最前面，扫描器要能越过它。
_WS = " \t\r\n\ufeff"


def _skip_ws(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in _WS:
        pos += 1
    return pos


def _scan_string(text: str, pos: int) -> int:
    """`pos` 指向开引号，返回闭引号的下一位。"""
    pos += 1
    while pos < len(text):
        char = text[pos]
        if char == "\\":
            pos += 2
            continue
        if char == '"':
            return pos + 1
        pos += 1
    raise ClientSettingsError("设置文件的字符串没有正常闭合")


def _scan_value_end(text: str, pos: int) -> int:
    """返回一个 JSON 值 token 的结束位置（不含结尾空白）。"""
    pos = _skip_ws(text, pos)
    if pos >= len(text):
        raise ClientSettingsError("设置文件在期望出现值的地方结束了")
    char = text[pos]
    if char == '"':
        return _scan_string(text, pos)
    if char == "{":
        return _scan_object(text, pos).close_pos + 1
    if char == "[":
        depth = 0
        index = pos
        while index < len(text):
            current = text[index]
            if current == '"':
                index = _scan_string(text, index)
                continue
            if current == "[":
                depth += 1
            elif current == "]":
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
        raise ClientSettingsError("设置文件的数组没有正常闭合")
    # 标量：扫到分隔符为止。
    index = pos
    while index < len(text) and text[index] not in _WS + ",}]":
        index += 1
    return index


def _scan_object(text: str, pos: int) -> _Object:
    """`pos` 指向 `{`，扫描出顶层成员表（含每个值的字节区间）。"""
    open_pos = pos
    index = _skip_ws(text, pos + 1)
    members: list[_Member] = []
    while True:
        index = _skip_ws(text, index)
        if index < len(text) and text[index] == "}":
            # 空对象与（非标准 JSON 的）尾逗号都在这里收口。
            return _Object(open_pos=open_pos, close_pos=index, members=tuple(members))
        if index >= len(text) or text[index] != '"':
            raise ClientSettingsError("设置文件的对象键必须是字符串")
        key_start = index
        key_end = _scan_string(text, index)
        key = json.loads(text[key_start:key_end])
        index = _skip_ws(text, key_end)
        if index >= len(text) or text[index] != ":":
            raise ClientSettingsError(f"设置文件的键 {key} 后面缺少冒号")
        value_start = _skip_ws(text, index + 1)
        value_end = _scan_value_end(text, value_start)
        members.append(_Member(key=str(key), value_start=value_start, value_end=value_end))
        index = _skip_ws(text, value_end)
        if index < len(text) and text[index] == ",":
            index += 1
            continue
        if index < len(text) and text[index] == "}":
            return _Object(open_pos=open_pos, close_pos=index, members=tuple(members))
        raise ClientSettingsError("设置文件的对象结构异常")


def _root_object(text: str) -> _Object:
    pos = _skip_ws(text, 0)
    if pos >= len(text) or text[pos] != "{":
        raise ClientSettingsError("设置文件的顶层不是 JSON 对象")
    return _scan_object(text, pos)


def _locate(text: str, path: tuple[str, ...]) -> _Member | None:
    """按键路径找成员；找不到返回 None。路径只可能在对象里逐层深入。"""
    current = _root_object(text)
    for depth, key in enumerate(path):
        found = next((item for item in current.members if item.key == key), None)
        if found is None:
            return None
        if depth == len(path) - 1:
            return found
        stripped = _skip_ws(text, found.value_start)
        if stripped >= len(text) or text[stripped] != "{":
            return None
        current = _scan_object(text, stripped)
    return None


# ---------- 指纹与最小补丁 ----------


def _strip_bom(text: str) -> str:
    return text[1:] if text.startswith(_BOM_CHAR) else text


def _indent_unit(text: str) -> str:
    """取第一处行首空白作为缩进风格；找不到就用两空格。"""
    for line in _strip_bom(text).splitlines()[1:]:
        if not line.strip():
            continue
        stripped = line.lstrip(" \t")
        if stripped and len(stripped) < len(line):
            return line[: len(line) - len(stripped)]
    return _INDENT_DEFAULT


def _eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _top_level_keys(text: str) -> tuple[str, ...]:
    try:
        return tuple(member.key for member in _root_object(text).members)
    except ClientSettingsError:
        return ()


def fingerprint(text: str) -> str:
    """格式指纹：缩进 + 行尾 + BOM + 顶层键序。**不含任何值**。

    只要格式或键序变了（例如被别的编辑器格式化过），指纹就变 —— 写入前
    比对失败即拒绝写。只改值时指纹不变，所以可以连续写多次。
    """
    material = "|".join(
        (
            repr(_indent_unit(text)),
            repr(_eol(text)),
            "bom" if text.startswith(_BOM_CHAR) else "nobom",
            ",".join(_top_level_keys(text)),
        )
    )
    return sha256(material.encode("utf-8")).hexdigest()


def _render(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _path_of(spec: SettingSpec) -> tuple[str, ...]:
    """规格里的键路径：`a.b` 表示顶层 `a` 里的 `b`（点分逐层深入）。"""
    return tuple(spec.key.split("."))


def _insert_member(text: str, spec: SettingSpec, value: Any) -> str:
    """键不存在时的最小插入：只在最后一个成员之后加一段，不动其它字节。

    只支持**顶层**键：嵌套键不存在时不自动创建（要建中间对象就得重排结构，
    那不叫最小补丁了），直接报错让用户先在客户端里把它设出来。
    """
    path = _path_of(spec)
    if len(path) > 1:
        raise ClientSettingsError(
            f"客户端文件里没有键 {spec.key}，嵌套键不会被自动创建；"
            "请先在客户端里设置一次，再回来改。"
        )
    root = _root_object(text)
    indent_unit = _indent_unit(text)
    eol = _eol(text)
    rendered = _render(value)
    member_indent = indent_unit
    if not root.members:
        # 空对象（`{}` 或 `{\n}`）：在开括号后插一行新成员，闭括号前不动。
        inner = f'{eol}{member_indent}"{spec.key}": {rendered}{eol}'
        return text[: root.open_pos + 1] + inner + text[root.close_pos:]
    last = root.members[-1]
    tail_start = _skip_ws(text, last.value_end)
    if tail_start < len(text) and text[tail_start] == ",":
        # 已有（非标准 JSON 的）尾逗号：插在逗号之后，不再补逗号。
        insert_at = tail_start + 1
        addition = f'{eol}{member_indent}"{spec.key}": {rendered}'
    else:
        insert_at = last.value_end
        addition = f'{eol}{member_indent}"{spec.key}": {rendered}'
        addition = "," + addition
    return text[:insert_at] + addition + text[insert_at:]


def patch_minimal(text: str, spec: SettingSpec, value: Any) -> str:
    """字节级最小补丁：只替换目标值 token（键不存在时最小插入）。

    返回新文本；除目标值那一段（或插入点）外，其余字符逐字节不变，
    含 BOM、缩进、行尾与键序。
    """
    member = _locate(text, _path_of(spec))
    if member is None:
        return _insert_member(text, spec, value)
    return text[: member.value_start] + _render(value) + text[member.value_end:]


def read_values(text: str) -> dict[str, tuple[bool, Any]]:
    """读出登记键的「是否存在 + 当前值」。未登记的键不读、不返回。"""
    result: dict[str, tuple[bool, Any]] = {}
    for spec in SPECS:
        member = _locate(text, _path_of(spec))
        if member is None:
            result[spec.key] = (False, None)
            continue
        try:
            parsed = json.loads(text[member.value_start : member.value_end])
        except json.JSONDecodeError:
            result[spec.key] = (True, None)
            continue
        result[spec.key] = (True, parsed)
    return result


# ---------- 服务 ----------


@dataclass(frozen=True, slots=True)
class BackupPoint:
    id: str
    size: int
    created_at: str

    def payload(self) -> dict[str, Any]:
        return {"id": self.id, "size": self.size, "createdAt": self.created_at}


class ClientSettingsService:
    """读 / 写 / 备份 / 恢复上游客户端设置文件。

    `settings_file` 只读写这一个文件；备份落在 `backups_dir`（我们自己的目录）。
    """

    def __init__(self, settings_file: Path, backups_dir: Path) -> None:
        self._file = settings_file
        self._backups = backups_dir
        self._lock = threading.RLock()

    # ---- 读 ----

    def _read_text(self) -> str | None:
        # 刻意用 "utf-8" 而不是 "utf-8-sig"：BOM 要作为字符留在文本里，
        # 这样指纹能识别它、写回时也能原样带回去。
        try:
            return self._file.read_bytes().decode("utf-8", "strict")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError):
            return None

    def snapshot(self, *, message: str = "") -> dict[str, Any]:
        text = self._read_text()
        if text is None:
            return {
                "available": False,
                "path": str(self._file),
                "fingerprint": None,
                "message": message or "读不到客户端设置文件（未安装，或文件不存在）",
                "specs": [
                    spec.payload(present=False, value=spec.default) for spec in SPECS
                ],
                "backups": [item.payload() for item in self.backups()],
            }
        values = read_values(text)
        return {
            "available": True,
            "path": str(self._file),
            "fingerprint": fingerprint(text),
            "message": message,
            "specs": [
                spec.payload(present=values[spec.key][0], value=values[spec.key][1])
                for spec in SPECS
            ],
            "backups": [item.payload() for item in self.backups()],
        }

    # ---- 写 ----

    def write(self, updates: dict[str, Any], confirm_fingerprint: str) -> dict[str, Any]:
        """按白名单改若干键。三道关：白名单 → 指纹 → 最小补丁（+ 先备份）。"""
        unknown = [key for key in updates if key not in SPEC_BY_KEY]
        if unknown:
            raise ClientSettingsError(
                f"未登记的设置项不会被改动：{'、'.join(sorted(unknown))}"
            )
        if not updates:
            raise ClientSettingsError("没有要修改的设置项")

        with self._lock:
            text = self._read_text()
            if text is None:
                raise ClientSettingsError("读不到客户端设置文件，无法写入")
            current = fingerprint(text)
            if confirm_fingerprint and confirm_fingerprint != current:
                raise ClientSettingsError(
                    "设置文件的格式在读取之后发生了变化，已拒绝写入。"
                    "请刷新后重新确认。"
                )

            before = read_values(text)
            patched = text
            changed: list[dict[str, Any]] = []
            for key, raw in updates.items():
                spec = SPEC_BY_KEY[key]
                value = _coerce(spec, raw)
                patched = patch_minimal(patched, spec, value)
                changed.append(
                    {"key": key, "label": spec.label, "before": before[key][1], "after": value}
                )

            if patched == text:
                return {
                    "changed": [],
                    "fingerprint": current,
                    "backupId": None,
                    "settings": self.snapshot(message="值没有变化，未写入"),
                }

            backup_id = self._backup(text)
            self._atomic_write(patched)
            fresh = read_values(patched)
            return {
                "changed": changed,
                "fingerprint": fingerprint(patched),
                "backupId": backup_id,
                "settings": {
                    "available": True,
                    "path": str(self._file),
                    "fingerprint": fingerprint(patched),
                    "message": "",
                    "specs": [
                        spec.payload(present=fresh[spec.key][0], value=fresh[spec.key][1])
                        for spec in SPECS
                    ],
                    "backups": [item.payload() for item in self.backups()],
                },
            }

    def _atomic_write(self, text: str) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_name(self._file.name + ".chaierww.tmp")
        try:
            tmp.write_bytes(text.encode("utf-8"))
            os.replace(tmp, self._file)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    # ---- 备份 ----

    def _backup(self, text: str) -> str:
        self._backups.mkdir(parents=True, exist_ok=True)
        stamp = _stamp()
        backup_id = f"{BACKUP_PREFIX}{stamp}"
        (self._backups / f"{backup_id}.json").write_bytes(text.encode("utf-8"))
        self._prune()
        return backup_id

    def backups(self) -> list[BackupPoint]:
        if not self._backups.is_dir():
            return []
        points: list[BackupPoint] = []
        for path in sorted(self._backups.glob(f"{BACKUP_PREFIX}*.json")):
            try:
                stat = path.stat()
            except OSError:
                continue
            points.append(
                BackupPoint(
                    id=path.stem,
                    size=stat.st_size,
                    created_at=_stamp_to_iso(path.stem),
                )
            )
        return points

    def _prune(self) -> None:
        points = self.backups()
        for stale in points[: max(0, len(points) - BACKUP_KEEP)]:
            try:
                (self._backups / f"{stale.id}.json").unlink()
            except OSError:
                continue

    def restore(self, backup_id: str) -> dict[str, Any]:
        """用备份点覆盖设置文件。恢复前先把当前内容再备份一次（恢复也不可逆）。"""
        source = self._backups / f"{backup_id}.json"
        if not source.is_file():
            raise ClientSettingsError(f"备份点不存在：{backup_id}")
        if not backup_id.startswith(BACKUP_PREFIX):
            raise ClientSettingsError(f"不是本工具生成的备份点：{backup_id}")

        with self._lock:
            current = self._read_text()
            if current is not None:
                self._backup(current)
            # 用 read_bytes 而不是 read_text：后者会把 CRLF 归一成 LF，破坏逐字节还原。
            content = source.read_bytes().decode("utf-8")
            self._atomic_write(content)
            return self.snapshot(message=f"已从备份点 {backup_id} 恢复")


def _stamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _stamp_to_iso(backup_id: str) -> str:
    from datetime import datetime

    raw = backup_id[len(BACKUP_PREFIX) :]
    for fmt in ("%Y%m%d-%H%M%S-%f", "%Y%m%d-%H%M%S"):
        try:
            return datetime.strptime(raw, fmt).isoformat(timespec="seconds")
        except ValueError:
            continue
    return ""

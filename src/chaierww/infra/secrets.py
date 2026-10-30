from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Protocol, runtime_checkable

INSECURE_ENV = "CHAIERWW_INSECURE_SECRETS"

#: Linux 密文前缀。换算法或换派生方式时必须换版本号，否则老密文会被当新格式解。
LINUX_CIPHER_PREFIX = b"lnx1:"
#: 密钥派生用的固定盐；它不是秘密，只是把本派生和其它程序的派生区分开。
_LINUX_KEY_SALT = b"chaierww/linux/aes-gcm/v1"
#: AES-GCM 的 nonce 长度（96 bit 是推荐值）。
_GCM_NONCE_SIZE = 12
#: 机器标识候选文件，按顺序取第一个读得到的。
_MACHINE_ID_PATHS = ("/etc/machine-id", "/var/lib/dbus/machine-id")


class SecretBoxError(RuntimeError):
    """凭据加解密失败。绝不把底层原因直接抛给用户界面。"""


class PlatformNotSupported(SecretBoxError):
    pass


@runtime_checkable
class SecretBox(Protocol):
    """凭据加解密入口。

    SPEC-M1a 要求：密文与密钥分离，密钥不落文件。
    Windows 用 DPAPI 由系统按当前用户派生密钥，天然满足该要求。
    Linux 用 :class:`LinuxAesBox`（machine-id + uid 派生，密钥不落盘）。
    其余平台未实现，调用即失败——而不是悄悄降级成不安全的方案。
    """

    def seal(self, plaintext: bytes) -> bytes: ...

    def open(self, ciphertext: bytes) -> bytes: ...

    def describe(self) -> str: ...


class InMemoryBox:
    """仅供测试：使用进程内临时密钥，重启即失效。"""

    def __init__(self, key: bytes | None = None) -> None:
        self._key = key or os.urandom(32)

    def _xor(self, data: bytes) -> bytes:
        stream = bytearray()
        counter = 0
        while len(stream) < len(data):
            digest = hashlib.sha256(self._key + counter.to_bytes(8, "big")).digest()
            stream.extend(digest)
            counter += 1
        return bytes(a ^ b for a, b in zip(data, stream, strict=False))

    def seal(self, plaintext: bytes) -> bytes:
        return b"mem1:" + self._xor(plaintext)

    def open(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(b"mem1:"):
            raise SecretBoxError("密文格式不匹配")
        return self._xor(ciphertext[5:])

    def describe(self) -> str:
        return "内存临时密钥（仅供测试）"


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _CRYPTPROTECT_UI_FORBIDDEN = 0x01

    class _Blob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_Blob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    def _make_blob(data: bytes) -> tuple[_Blob, ctypes.Array[ctypes.c_char]]:
        buffer = ctypes.create_string_buffer(data, len(data))
        blob = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        return blob, buffer

    def _blob_bytes(blob: _Blob) -> bytes:
        return ctypes.string_at(blob.pbData, blob.cbData)

    class WindowsDpapiBox:
        """用 Windows DPAPI 加解密：密钥由系统按当前登录用户派生，不落盘。"""

        def seal(self, plaintext: bytes) -> bytes:
            in_blob, _buffer = _make_blob(plaintext)
            out_blob = _Blob()
            ok = _crypt32.CryptProtectData(
                ctypes.byref(in_blob),
                None,
                None,
                None,
                None,
                _CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(out_blob),
            )
            if not ok:
                raise SecretBoxError(f"加密失败（错误码 {ctypes.get_last_error()}）")
            try:
                return _blob_bytes(out_blob)
            finally:
                _kernel32.LocalFree(out_blob.pbData)

        def open(self, ciphertext: bytes) -> bytes:
            in_blob, _buffer = _make_blob(ciphertext)
            out_blob = _Blob()
            ok = _crypt32.CryptUnprotectData(
                ctypes.byref(in_blob),
                None,
                None,
                None,
                None,
                _CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(out_blob),
            )
            if not ok:
                raise SecretBoxError(
                    f"解密失败（错误码 {ctypes.get_last_error()}）；密文可能由其他用户或机器生成"
                )
            try:
                return _blob_bytes(out_blob)
            finally:
                _kernel32.LocalFree(out_blob.pbData)

        def describe(self) -> str:
            return "Windows DPAPI（按当前用户加密）"


def _read_machine_id() -> str:
    """读本机 machine-id：systemd 与 dbus 两个位置，取第一个非空的。"""
    for candidate in _MACHINE_ID_PATHS:
        try:
            text = Path(candidate).read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            continue
        if text:
            return text
    raise SecretBoxError(
        "读不到本机 machine-id（/etc/machine-id 或 /var/lib/dbus/machine-id），"
        "无法派生 Linux 凭据密钥"
    )


def _current_uid() -> int:
    """当前进程的 uid；Windows 上没有这个接口，测试里会显式注入。"""
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if getuid else 0


class LinuxAesBox:
    """Linux 凭据加密：AES-256-GCM，密钥由 machine-id + uid 派生，不落盘。

    能力边界**如实写在这里**，不要在界面上说成「和 Windows 一样安全」：

    - 挡得住「密文被拷走」：换台机器或换个用户，派生不出同一把密钥，解不开；
    - 挡不住本机 root：机器标识、库文件、进程内存都在 root 手里，本机主权的场景
      没有可用的系统级密钥服务（CentOS 7 尤其没有 Secret Service）；
    - 挡不住删除重建机器标识（``/etc/machine-id`` 被重置后旧密文全部失效，
      届时表现为「凭据无法解密」，用户需要重新导入账号）。

    ``machine_id`` / ``uid`` 允许注入，仅用于在 Windows 上跑单测。
    """

    def __init__(self, machine_id: str | None = None, uid: int | None = None) -> None:
        self._machine_id = machine_id if machine_id is not None else _read_machine_id()
        self._uid = uid if uid is not None else _current_uid()
        material = (
            _LINUX_KEY_SALT
            + self._machine_id.encode("utf-8")
            + b"\x00"
            + str(self._uid).encode("ascii")
        )
        self._key = hashlib.sha256(material).digest()

    def seal(self, plaintext: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(_GCM_NONCE_SIZE)
        try:
            body = AESGCM(self._key).encrypt(nonce, plaintext, None)
        except Exception as exc:  # noqa: BLE001 - 底层异常不外泄
            raise SecretBoxError("Linux 凭据加密失败") from exc
        return LINUX_CIPHER_PREFIX + nonce + body

    def open(self, ciphertext: bytes) -> bytes:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if not ciphertext.startswith(LINUX_CIPHER_PREFIX):
            raise SecretBoxError("密文格式不匹配")
        payload = ciphertext[len(LINUX_CIPHER_PREFIX) :]
        if len(payload) < _GCM_NONCE_SIZE + 16:
            raise SecretBoxError("密文长度不合法")
        nonce, body = payload[:_GCM_NONCE_SIZE], payload[_GCM_NONCE_SIZE:]
        try:
            return AESGCM(self._key).decrypt(nonce, body, None)
        except InvalidTag as exc:
            raise SecretBoxError(
                "解密失败：密文可能来自另一台机器 / 另一个用户，"
                "或本机 machine-id 已变更"
            ) from exc

    def describe(self) -> str:
        return "Linux AES-256-GCM（按机器 + 用户派生，不防本机 root）"


class InsecureDevBox(InMemoryBox):
    """显式开启的危险占位实现，仅用于非 Windows 开发调试。"""

    def describe(self) -> str:
        return "不安全开发占位（密钥在进程内存，重启丢失）"


def default_box(allow_insecure_dev: bool | None = None) -> SecretBox:
    if sys.platform == "win32":
        return WindowsDpapiBox()

    if allow_insecure_dev is None:
        allow_insecure_dev = os.environ.get(INSECURE_ENV) == "1"
    if allow_insecure_dev:
        return InsecureDevBox()

    if sys.platform.startswith("linux"):
        return LinuxAesBox()

    raise PlatformNotSupported(
        f"当前平台（{sys.platform}）尚未实现凭据加密。"
        f"开发调试可设置 {INSECURE_ENV}=1 启用不安全占位实现。"
    )

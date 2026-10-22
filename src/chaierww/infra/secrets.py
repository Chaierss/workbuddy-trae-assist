from __future__ import annotations

import hashlib
import os
import sys
from typing import Protocol, runtime_checkable

INSECURE_ENV = "CHAIERWW_INSECURE_SECRETS"


class SecretBoxError(RuntimeError):
    """凭据加解密失败。绝不把底层原因直接抛给用户界面。"""


class PlatformNotSupported(SecretBoxError):
    pass


@runtime_checkable
class SecretBox(Protocol):
    """凭据加解密入口。

    SPEC-M1a 要求：密文与密钥分离，密钥不落文件。
    Windows 用 DPAPI 由系统按当前用户派生密钥，天然满足该要求。
    其他平台未实现，调用即失败——而不是悄悄降级成不安全的方案。
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

    raise PlatformNotSupported(
        f"当前平台（{sys.platform}）尚未实现凭据加密。"
        f"开发调试可设置 {INSECURE_ENV}=1 启用不安全占位实现。"
    )

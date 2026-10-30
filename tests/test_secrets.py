"""Linux 凭据加密单测（0.12.0）。

Windows 上没有 ``/etc/machine-id``，所以 ``LinuxAesBox`` 的构造参数允许注入
``machine_id`` / ``uid`` —— 密钥派生逻辑与平台无关，注入后就能在任何平台验证。
"""

from __future__ import annotations

import pytest

from chaierww.infra import secrets

MACHINE = "0123456789abcdef0123456789abcdef"


def _box(machine_id: str = MACHINE, uid: int = 1000) -> secrets.LinuxAesBox:
    return secrets.LinuxAesBox(machine_id=machine_id, uid=uid)


def test_roundtrip_preserves_plaintext():
    box = _box()
    plaintext = b"\xef\xbb\xbf" + b'{"auth":{"accessToken":"tok"}}'
    ciphertext = box.seal(plaintext)

    assert ciphertext.startswith(secrets.LINUX_CIPHER_PREFIX)
    assert plaintext not in ciphertext
    assert box.open(ciphertext) == plaintext


def test_ciphertext_is_not_deterministic():
    """同一明文两次封箱必须不同：GCM 每次随机 nonce，否则密文可被比对。"""
    box = _box()
    assert box.seal(b"same") != box.seal(b"same")


def test_other_machine_cannot_open():
    ciphertext = _box(machine_id=MACHINE).seal(b"secret")
    with pytest.raises(secrets.SecretBoxError, match="解密失败"):
        _box(machine_id="ffffffffffffffffffffffffffffffff").open(ciphertext)


def test_other_user_cannot_open():
    ciphertext = _box(uid=1000).seal(b"secret")
    with pytest.raises(secrets.SecretBoxError, match="解密失败"):
        _box(uid=1001).open(ciphertext)


def test_open_rejects_foreign_ciphertext():
    with pytest.raises(secrets.SecretBoxError, match="密文格式不匹配"):
        _box().open(b"garbage")


def test_open_rejects_truncated_payload():
    box = _box()
    ciphertext = box.seal(b"secret")
    with pytest.raises(secrets.SecretBoxError, match="长度不合法"):
        box.open(ciphertext[: len(secrets.LINUX_CIPHER_PREFIX) + 10])


def test_describe_states_the_boundary():
    """describe 会被界面展示，必须诚实说明「不防本机 root」，不能吹成 DPAPI。"""
    text = _box().describe()
    assert "Linux" in text
    assert "root" in text

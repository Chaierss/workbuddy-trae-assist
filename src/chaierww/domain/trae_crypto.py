"""Trae 设备凭据（tc 信封）解密。

Trae 国内版把设备身份 / 登录态副本存在 ``storage.json`` 的 ``iCubeAuthInfo*`` 键里，
值是被自研 ``tc`` 信封包裹的 base64（magic ``74 63 05 10 00 00``）。信封密钥派生用到的
pepper 是**随安装包分发的 4 张 64B 公开常量表**（混淆，不是加密），因此本工具能离线解密。

协议见 upstream 的 ``icube.rs``——https://github.com/NextAgentX/qoder-trae-workbuddy-switch
（Rust/Tauri 实现，与本项目同一许可，Required Notice 见仓库根 LICENSE）：2026-10-19 已用
本机 Trae 数据实测，成功解出 EC P-256 公私钥。

算法（byteCrypto）：

1. base64 解码 → 校验前 6B magic == ``74 63 05 10 00 00``；
2. 取 ``random = raw[6:38]``（32B 随机盐）、``cipher = raw[38:]``；
3. 密钥派生 ``derive_keys``：``SHA512(SHA512(random) ‖ pepper)`` 前 32B，
   其中 ``key = digest[:16]``、``iv = digest[16:32]``，pepper = ``WOE_T ^ VOE_T``；
4. AES-128-CBC（PKCS7）解密；
5. 明文前 64B 是 ``SHA512(body)`` 完整性校验，比对通过后返回 body 文本。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# magic：hex 746305100000
TC_HEADER = bytes([116, 99, 5, 16, 0, 0])
HEADER_LEN = 6
RANDOM_LEN = 32
SHA512_LEN = 64

# byteCrypto 四张 64B 公开常量表（随安装包分发，混淆非加密）。
WOE_T = bytes(
    [
        82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251, 124, 227, 57, 130,
        155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203, 84, 123, 148, 50, 166, 194, 35, 61,
        238, 76, 149, 11, 66, 250, 195, 78, 8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73,
        109, 139, 209, 37,
    ]
)
VOE_T = bytes(
    [
        31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95, 96, 81, 127, 169, 25,
        181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239, 160, 224, 59, 77, 174, 42, 245, 176,
        200, 235, 187, 60, 131, 83, 153, 97, 23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99,
        85, 33, 12, 125,
    ]
)
JOE_T = bytes(
    [
        191, 192, 216, 250, 122, 246, 220, 97, 31, 254, 98, 27, 8, 72, 71, 176, 135, 99, 96, 18,
        127, 101, 203, 104, 211, 102, 191, 125, 37, 72, 150, 156, 51, 229, 121, 35, 17, 153, 141,
        177, 110, 131, 150, 128, 172, 255, 254, 6, 18, 140, 55, 62, 236, 249, 135, 64, 135, 12,
        117, 4, 89, 149, 168, 209,
    ]
)
HOE_T = bytes(
    [
        246, 204, 26, 232, 232, 70, 129, 109, 223, 146, 169, 242, 23, 241, 105, 145, 50, 196, 165,
        42, 254, 120, 3, 54, 244, 207, 209, 85, 53, 6, 138, 106, 175, 148, 31, 204, 186, 186, 165,
        182, 87, 142, 49, 10, 39, 110, 26, 154, 86, 56, 173, 125, 18, 64, 198, 225, 99, 99, 83, 82,
        191, 134, 76, 170,
    ]
)

ICUBE_DC_PREFIX = "iCubeAuthInfo://icube-dc:"
TELEMETRY_MACHINE_ID = "telemetry.machineId"


class TraeCryptoError(RuntimeError):
    """tc 信封解密失败。消息面向用户，不含任何凭据片段。"""


def _aes_pepper() -> bytes:
    """AES 模式 pepper = ``WOE_T ^ VOE_T``。"""
    return bytes(a ^ b for a, b in zip(WOE_T, VOE_T, strict=True))


def _derive_keys(random: bytes, pepper: bytes) -> tuple[bytes, bytes]:
    """密钥派生：``SHA512(SHA512(random) ‖ pepper)`` 前 32B，切 key/iv。"""
    first = hashlib.sha512(random).digest()
    digest = hashlib.sha512(first + pepper).digest()
    return digest[:16], digest[16:32]


def tc_decrypt(b64: str) -> str:
    """解密一条 ``tc`` 信封，返回明文 JSON 文本。

    只支持 AES 模式（本机 Trae 数据实测两键均为 AES，``AES_PRIVATE`` 未使用）。
    """
    try:
        raw = base64.b64decode(b64.strip())
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise TraeCryptoError(f"设备凭证 base64 解码失败：{exc}") from exc

    if len(raw) < HEADER_LEN + RANDOM_LEN + 16:
        raise TraeCryptoError(f"设备凭证长度异常：{len(raw)}")
    if raw[:HEADER_LEN] != TC_HEADER:
        raise TraeCryptoError(
            "设备凭证 magic 不匹配（期望 746305100000），客户端可能刚升级过"
        )

    random = raw[HEADER_LEN:HEADER_LEN + RANDOM_LEN]
    cipher = raw[HEADER_LEN + RANDOM_LEN:]
    key, iv = _derive_keys(random, _aes_pepper())

    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(cipher) + decryptor.finalize()

    # 去 PKCS7 padding
    pad_len = padded[-1]
    if pad_len < 1 or pad_len > 16:
        raise TraeCryptoError("设备凭证解密失败（填充异常）")
    unpadded = padded[:-pad_len]

    if len(unpadded) < SHA512_LEN:
        raise TraeCryptoError("设备凭证解密后明文过短")
    tag, body = unpadded[:SHA512_LEN], unpadded[SHA512_LEN:]
    if hashlib.sha512(body).digest() != tag:
        raise TraeCryptoError("设备凭证完整性校验失败（SHA512 不匹配）")

    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TraeCryptoError("设备凭证明文非 UTF-8") from exc


def tc_encrypt(plain: str) -> str:
    """把明文 JSON 文本封装成一条 ``tc`` 信封（``tc_decrypt`` 的精确逆操作）。

    用于把扫码采集的明文 JWT 账号反向构造成客户端能认的
    ``iCubeAuthInfo://icube.cloudide`` 信封，实现真正的 Trae token 切换。

    算法：
    1. ``body = plain.encode('utf-8')``，``tag = SHA512(body)``（64B）；
    2. ``unpadded = tag + body``，PKCS7 填充到 16 的整数倍；
    3. 生成 32B 随机盐 ``random``，``key, iv = derive_keys(random, pepper)``；
    4. AES-128-CBC 加密；
    5. 输出 ``base64(magic + random + cipher)``。
    """
    body = plain.encode("utf-8")
    tag = hashlib.sha512(body).digest()
    unpadded = tag + body

    pad_len = 16 - (len(unpadded) % 16)
    padded = unpadded + bytes([pad_len]) * pad_len

    random = os.urandom(RANDOM_LEN)
    key, iv = _derive_keys(random, _aes_pepper())
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    cipher = encryptor.update(padded) + encryptor.finalize()

    raw = TC_HEADER + random + cipher
    return base64.b64encode(raw).decode("ascii")


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """Trae 设备身份（OAuth 授权 URL 与 ExchangeToken 请求体的同源取值来源）。

    刻意**不含私钥**：AuthCode 交换路径不需要签名，持有私钥反而有泄漏风险。
    """

    device_id: str
    machine_id: str
    public_key_pem: str


def device_identity_from_storage(storage: dict) -> DeviceIdentity:
    """从 ``storage.json`` 解析出的对象里取设备身份。

    - ``device_id``：``iCubeAuthInfo://icube-dc:<deviceId>`` 键名内嵌的 id；
    - ``public_key_pem``：该键值 tc 解密后的 ``publicKeyPEM``；
    - ``machine_id``：同文件 ``telemetry.machineId``（取不到为空串）。
    """
    device_id: str | None = None
    b64: str | None = None
    for key, value in storage.items():
        if str(key).startswith(ICUBE_DC_PREFIX):
            device_id = str(key)[len(ICUBE_DC_PREFIX):]
            if isinstance(value, str):
                b64 = value
            break

    if not device_id or not b64:
        raise TraeCryptoError(
            "storage.json 中没有设备身份（iCubeAuthInfo://icube-dc），"
            "请先启动一次 Trae 客户端并登录"
        )

    plain = json.loads(tc_decrypt(b64))
    public_key = (plain.get("publicKeyPEM") or "").strip()
    if not public_key:
        raise TraeCryptoError("设备凭证缺少公钥（信封形态可能已变化）")

    machine_id = storage.get(TELEMETRY_MACHINE_ID)
    if not isinstance(machine_id, str):
        machine_id = ""

    return DeviceIdentity(
        device_id=device_id,
        machine_id=machine_id,
        public_key_pem=public_key,
    )

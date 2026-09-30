"""
B 站 Cookie 管理模块。

支持三种 Cookie 获取方式：
1. 二维码登录 —— 生成二维码图片供扫码
2. 手动输入 —— 通过 /设置cookie 命令
3. CookieCloud（可选支持）—— 从已配置的 CookieCloud 服务同步
"""

import hashlib
import json
import time
from base64 import b64decode
from io import StringIO
from pathlib import Path
from typing import Optional

import httpx
import qrcode
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .config import ConfigManager


class CookieManager:
    """Cookie 管理器，封装二维码生成、CookieCloud 解密和读写操作。"""

    # ------------------------------------------------------------------
    # 二维码登录
    # ------------------------------------------------------------------

    @classmethod
    def _build_login_url(cls) -> str:
        """生成 B 站登录二维码链接。"""
        return (
            f"https://passport.bilibili.com/qrcode/h5/login"
            f"?navhide=1&t={int(time.time())}"
        )

    @classmethod
    def _build_qrcode(cls, qr_data: str) -> qrcode.QRCode:
        """根据登录链接构造二维码对象，供图片和终端输出复用。"""
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=2,
            border=2,
        )
        qr.add_data(qr_data)
        qr.make(fit=True)
        return qr

    @classmethod
    def _generate_qrcode_assets(cls) -> tuple[str, qrcode.QRCode, str]:
        """一次性生成登录链接、二维码对象和二维码图片。"""
        qr_data = cls._build_login_url()
        qr = cls._build_qrcode(qr_data)
        img = qr.make_image(fill_color="black", back_color="white")

        runtime_path = Path("data/runtime")
        runtime_path.mkdir(parents=True, exist_ok=True)
        img_path = runtime_path / "bili_qrcode.png"
        img.save(img_path)
        return qr_data, qr, str(img_path)

    @classmethod
    def generate_qrcode(cls) -> tuple[str, str]:
        """
        生成 B 站登录二维码。

        返回 (登录链接, 图片保存路径)。
        """
        qr_data, _, img_path = cls._generate_qrcode_assets()
        return qr_data, str(img_path)

    @classmethod
    def generate_terminal_qrcode(cls) -> tuple[str, str, str]:
        """
        生成终端可显示的 ASCII 二维码。

        返回 (登录链接, 图片保存路径, 终端二维码文本)。
        """
        qr_data, qr, img_path = cls._generate_qrcode_assets()
        buffer = StringIO()
        qr.print_ascii(out=buffer, invert=True)
        return qr_data, img_path, buffer.getvalue()

    @classmethod
    def has_cookiecloud_config(cls, host: str, uuid: str, key: str) -> bool:
        """判断 CookieCloud 配置是否看起来已经由用户真实填写。"""
        values = [str(host).strip(), str(uuid).strip(), str(key).strip()]
        if not all(values):
            return False
        placeholder_markers = ("你的", "UUID", "密钥", "CookieCloud地址")
        return not any(any(marker in value for marker in placeholder_markers) for value in values)

    # ------------------------------------------------------------------
    # CookieCloud
    # ------------------------------------------------------------------

    @classmethod
    def _build_cookiecloud_key(cls, uuid: str, password: str) -> bytes:
        """按 CookieCloud 官方规则生成解密主密钥。"""
        hash_str = hashlib.md5(f"{uuid}-{password}".encode()).hexdigest()
        return hash_str[:16].encode()

    @classmethod
    def _evp_bytes_to_key(cls, password: bytes, salt: bytes, key_len: int, iv_len: int) -> tuple[bytes, bytes]:
        """兼容 CookieCloud legacy 模式使用的 OpenSSL EVP_BytesToKey 派生方式。"""
        key_iv = b""
        prev = b""
        while len(key_iv) < key_len + iv_len:
            prev = hashlib.md5(prev + password + salt).digest()
            key_iv += prev
        return key_iv[:key_len], key_iv[key_len:key_len + iv_len]

    @classmethod
    def _decrypt_cookiecloud(
        cls, encrypted_data: str, uuid: str, password: str, crypto_type: str = "legacy"
    ) -> dict:
        """按 CookieCloud 官方示例解密返回数据。"""
        key = cls._build_cookiecloud_key(uuid, password)
        encrypted_bytes = b64decode(encrypted_data)

        if crypto_type == "aes-128-cbc-fixed":
            cipher = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16))
            decryptor = cipher.decryptor()
            decrypted = decryptor.update(encrypted_bytes) + decryptor.finalize()
        else:
            salt = encrypted_bytes[8:16]
            ciphertext = encrypted_bytes[16:]
            aes_key, iv = cls._evp_bytes_to_key(key, salt, 32, 16)
            cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
            decryptor = cipher.decryptor()
            decrypted = decryptor.update(ciphertext) + decryptor.finalize()

        unpadder = padding.PKCS7(128).unpadder()
        unpadded = unpadder.update(decrypted) + unpadder.finalize()
        return json.loads(unpadded.decode("utf-8"))

    @classmethod
    def _extract_bilibili_cookie(cls, cookie_data: object) -> Optional[str]:
        """兼容不同 CookieCloud 数据结构，提取 bilibili.com Cookie。"""
        if isinstance(cookie_data, dict):
            for domain, cookies in cookie_data.items():
                if "bilibili.com" not in str(domain):
                    continue
                if not isinstance(cookies, list):
                    continue
                cookie_pairs = [
                    f"{cookie['name']}={cookie['value']}"
                    for cookie in cookies
                    if isinstance(cookie, dict) and cookie.get("name") and cookie.get("value")
                ]
                if cookie_pairs:
                    return "; ".join(cookie_pairs)

        if isinstance(cookie_data, list):
            for domain_group in cookie_data:
                if not isinstance(domain_group, dict):
                    continue
                domain = domain_group.get("domain", "")
                if "bilibili.com" not in domain:
                    continue
                cookies = domain_group.get("cookies", [])
                cookie_pairs = [
                    f"{cookie['name']}={cookie['value']}"
                    for cookie in cookies
                    if isinstance(cookie, dict) and cookie.get("name") and cookie.get("value")
                ]
                if cookie_pairs:
                    return "; ".join(cookie_pairs)

        return None

    @classmethod
    async def fetch_from_cookiecloud(
        cls, host: str, uuid: str, key: str
    ) -> Optional[str]:
        """
        从 CookieCloud 服务获取 B 站 Cookie。

        参数：
            host: CookieCloud 服务地址
            uuid: CookieCloud 用户 UUID
            key: 解密密钥

        返回：拼接后的 Cookie 字符串，失败返回 None。
        """
        normalized_host = host.rstrip("/")
        url = f"{normalized_host}/get/{uuid}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
        except Exception:
            return None

        # CookieCloud 响应中 encrypted 字段存储加密数据
        encrypted_data = data.get("encrypted") or data.get("data")
        if not encrypted_data:
            return None

        crypto_type = data.get("crypto_type") or "legacy"
        try:
            decrypted = cls._decrypt_cookiecloud(encrypted_data, uuid, key, crypto_type)
        except Exception:
            return None

        return cls._extract_bilibili_cookie(decrypted.get("cookie_data", []))

    # ------------------------------------------------------------------
    # Cookie 读写
    # ------------------------------------------------------------------

    @classmethod
    def set_cookie(cls, cookie: str) -> None:
        """将 Cookie 写入持久化配置。"""
        ConfigManager.set_bili_cookie(cookie)

    @classmethod
    def get_cookie(cls) -> str:
        """从持久化配置读取 Cookie。"""
        return ConfigManager.get_bili_cookie()

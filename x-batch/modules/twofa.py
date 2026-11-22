"""2FA / TOTP 验证码。

表里给的是形如 https://2fa.xdgongju.top/?key=XXXX 的链接，其中 key 就是
Base32 的 TOTP 密钥。优先本地按标准 TOTP(30 秒 / 6 位 / SHA1) 计算，
不需要联网；若链接里没有可用密钥，再退化为抓取网页上的 6 位数字。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct
import time
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


class TwoFAError(RuntimeError):
    """获取验证码失败。"""


_B32_RE = re.compile(r"^[A-Z2-7]{16,}$")


def _normalize_secret(text: str) -> str:
    secret = re.sub(r"[\s\-=]", "", (text or "")).upper()
    if not _B32_RE.match(secret):
        raise TwoFAError(f"不是合法的 Base32 密钥: {(text or '')[:12]}")
    return secret


def totp(secret: str, at: Optional[float] = None, period: int = 30, digits: int = 6) -> str:
    """按 RFC 6238 生成当前验证码。"""
    secret = _normalize_secret(secret)
    key = base64.b32decode(secret + "=" * ((-len(secret)) % 8))
    counter = int((time.time() if at is None else at) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def _secret_from_link(link: str) -> str:
    parsed = urlparse(link)
    params = parse_qs(parsed.query)
    for key in ("key", "secret", "k", "token"):
        values = params.get(key)
        if values and values[0].strip():
            return values[0].strip()
    tail = parsed.path.rstrip("/").split("/")[-1]
    return tail.strip() if tail else ""


def _code_from_web(link: str, timeout: float) -> str:
    request = Request(link, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            html = response.read().decode("utf-8", "ignore")
    except Exception as exc:  # 网络错误、Cloudflare 拦截等
        raise TwoFAError(f"打开 2FA 链接失败: {exc}") from exc
    for pattern in (r">\s*(\d{6})\s*<", r"\b(\d{6})\b"):
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    raise TwoFAError("2FA 页面里没有找到 6 位验证码")


def code_from_link(link: str, timeout: float = 10.0) -> Tuple[str, str]:
    """返回 (验证码, 来源)。来源为“本地计算”或“网页抓取”。"""
    link = (link or "").strip()
    if not link:
        raise TwoFAError("2FA 链接为空")

    secret = _secret_from_link(link)
    if secret:
        try:
            return totp(secret), "本地计算"
        except TwoFAError:
            pass
        except Exception:
            pass

    return _code_from_web(link, timeout), "网页抓取"

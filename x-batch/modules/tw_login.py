"""Twitter 登录：把 auth_token 写入浏览器 Cookie，再根据页面状态判定结果。"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page

log = logging.getLogger(__name__)

ACCOUNT_SELECTOR = '[data-testid="SideNav_AccountSwitcher_Button"]'
LOGIN_IN_URLS = ("/i/flow/login", "/login", "/i/flow/signup")
RESTRICTED_URLS = ("/account/access", "/i/flow/verify", "/i/flow/phone-verification")

# 返回状态
OK = "ok"
INVALID = "invalid"
RESTRICTED = "restricted"
ERROR = "error"


@dataclass
class LoginResult:
    status: str
    username: str = ""
    detail: str = ""


def _clear_auth_cookies(context: BrowserContext) -> None:
    for name in ("auth_token", "ct0", "twid"):
        try:
            context.clear_cookies(name=name)
        except Exception:
            pass


def _build_cookies(token: str, ct0: str) -> list[dict]:
    expires = int(time.time()) + 2 * 365 * 24 * 3600
    cookies = [
        {
            "name": "auth_token",
            "value": token,
            "domain": ".x.com",
            "path": "/",
            "expires": expires,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }
    ]
    if ct0:
        cookies.append(
            {
                "name": "ct0",
                "value": ct0,
                "domain": ".x.com",
                "path": "/",
                "expires": expires,
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            }
        )
    return cookies


def _has_auth_cookie(context: BrowserContext) -> bool:
    try:
        return any(
            c.get("name") == "auth_token" and c.get("value")
            for c in context.cookies("https://x.com")
        )
    except Exception:
        # 读不到时不做“失效”判断，避免误判
        return True


def _probe(page: Page) -> str:
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""

    if any(p in url for p in RESTRICTED_URLS):
        return RESTRICTED
    if any(p in url for p in LOGIN_IN_URLS):
        return INVALID

    try:
        if page.locator(ACCOUNT_SELECTOR).count() > 0:
            return OK
    except Exception:
        pass

    try:
        body = (page.inner_text("body", timeout=1500) or "").lower()
    except Exception:
        body = ""

    if "account is suspended" in body or "账号已被冻结" in body:
        return RESTRICTED
    if "verify your identity" in body or "确认你的身份" in body:
        return RESTRICTED
    # 登录页通常同时出现 Log in / Sign up，且页面文案很短
    if "sign up" in body and "log in" in body and len(body) < 4000:
        return INVALID
    return "unknown"


def _read_username(page: Page) -> str:
    # 侧边栏"个人资料"链接的 href 就是 @handle，最可靠
    try:
        href = page.locator(
            'a[data-testid="AppTabBar_Profile_Link"]'
        ).first.get_attribute("href", timeout=2000)
        if href:
            handle = href.strip("/").split("/")[0]
            if re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
                return "@" + handle
    except Exception:
        pass

    for selector in (ACCOUNT_SELECTOR, '[aria-label="Account menu"]'):
        try:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            text = locator.inner_text(timeout=2000)
            match = re.search(r"@([A-Za-z0-9_]{1,15})", text)
            if match:
                return "@" + match.group(1)
        except Exception:
            continue
    return ""


def _get_page(context: BrowserContext) -> Page:
    for page in context.pages:
        if not page.is_closed():
            return page
    return context.new_page()


def login_with_token(
    context: BrowserContext,
    token: str,
    home_url: str = "https://x.com/home",
    timeout: int = 25,
    ct0: str = "",
) -> LoginResult:
    """注入单个 auth_token 并判定：ok / invalid / restricted / error。"""
    token = (token or "").strip()
    if not token:
        return LoginResult(INVALID, detail="token 为空")

    _clear_auth_cookies(context)
    context.add_cookies(_build_cookies(token, ct0))

    page = _get_page(context)
    started = time.time()
    try:
        page.goto(home_url, wait_until="domcontentloaded", timeout=timeout * 1000)
    except Exception as exc:
        return LoginResult(ERROR, detail=f"打开 x.com 失败: {exc}")

    deadline = started + timeout
    reloaded = False
    while time.time() < deadline:
        state = _probe(page)

        if state == OK:
            username = _read_username(page)
            # 给 ixBrowser 一点时间把 cookie 写入磁盘
            time.sleep(1.0)
            return LoginResult(OK, username=username, detail="已登录")

        if state == INVALID:
            return LoginResult(INVALID, detail="被重定向到登录页")

        if state == RESTRICTED:
            return LoginResult(RESTRICTED, detail="账号受限/需要验证")

        # 注入 4 秒后 cookie 被清掉，基本可以判定 token 无效
        if time.time() - started > 4 and not _has_auth_cookie(context):
            return LoginResult(INVALID, detail="auth_token 未被服务端接受")

        # 8 秒还没结果就刷新一次，处理偶发的首屏白屏
        if not reloaded and time.time() - started > 8:
            reloaded = True
            try:
                page.reload(wait_until="domcontentloaded", timeout=timeout * 1000)
            except Exception:
                pass

        time.sleep(1.0)

    if not _has_auth_cookie(context):
        return LoginResult(INVALID, detail="auth_token 已被清除")
    return LoginResult(ERROR, detail="判定超时，未出现登录标识")

"""钱包：本地生成助记词 -> 自动导入 MetaMask；失败可降级为人工。"""
from __future__ import annotations

import logging
import re
import secrets
import string
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Tuple

from .config import WalletCfg

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page

log = logging.getLogger(__name__)

try:  # 允许在没装 eth-account 时先跑 Twitter 部分
    from eth_account import Account
    from mnemonic import Mnemonic
    _WALLET_DEPS_OK = True
except ImportError:  # pragma: no cover
    _WALLET_DEPS_OK = False

EXT_RE = re.compile(r"chrome-extension://([a-p]{32})")

# MetaMask 不同版本的选择器
SEL_IMPORT_ENTRY = '[data-testid="onboarding-import-wallet"]'
SEL_SRP_TEXTAREA = '[data-testid="import-srp__srp-note"]'
SEL_SRP_WORD = '[data-testid="import-srp__srp-word-{}"]'
SEL_SRP_CONFIRM = '[data-testid="import-srp-confirm"]'
SEL_PWD_NEW = '[data-testid="create-password-new"]'
SEL_PWD_CONFIRM = '[data-testid="create-password-confirm"]'
SEL_PWD_TERMS = '[data-testid="create-password-terms"]'
SEL_PWD_SUBMIT = '[data-testid="create-password-wallet"]'
SEL_READY = [
    '[data-testid="metamask-onboarding-wallet-ready-done"]',
    '[data-testid="onboarding-complete-done"]',
]
SEL_UNLOCK = '[data-testid="unlock-password"]'
SEL_HOME = '[data-testid="account-menu-icon"]'


@dataclass
class WalletResult:
    status: str  # ok / manual / exists / failed / skip
    address: str = ""
    mnemonic: str = ""
    password: str = ""
    note: str = ""


# ---------------------------------------------------------------- 生成钱包
def make_password(prefix: str, length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    body_len = max(4, length - len(prefix) - 4)
    body = "".join(secrets.choice(alphabet) for _ in range(body_len))
    # 保证包含大写、小写、数字、符号
    return f"{prefix}{body}!aA9"


def generate_wallet(password_prefix: str, password_length: int) -> Tuple[str, str, str]:
    if not _WALLET_DEPS_OK:
        raise RuntimeError(
            "缺少钱包依赖，请执行: pip install mnemonic eth-account"
        )
    mnemonic = Mnemonic("english").generate(128)  # 12 个单词
    # eth-account 默认关闭助记词功能，需显式开启
    Account.enable_unaudited_hdwallet_features()
    account = Account.from_mnemonic(mnemonic)
    return mnemonic, account.address, make_password(password_prefix, password_length)


# ------------------------------------------------------------- 扩展 ID 探测
def find_extension_id(
    browser: Browser, context: BrowserContext, fallback: str = ""
) -> str:
    if fallback:
        return fallback

    # 1) service worker 的 URL 里带扩展 ID
    try:
        for ctx in browser.contexts:
            for worker in ctx.service_workers:
                match = EXT_RE.match(worker.url or "")
                if match and "metamask" in (worker.url or "").lower():
                    return match.group(1)
    except Exception:
        pass

    # 2) 打开 chrome://extensions 读扩展列表
    page = None
    try:
        page = context.new_page()
        page.goto("chrome://extensions/", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(800)
        items = page.evaluate(
            """() => {
                const mgr = document.querySelector('extensions-manager');
                if (!mgr || !mgr.shadowRoot) return [];
                const list = mgr.shadowRoot.querySelector('extensions-item-list');
                if (!list || !list.shadowRoot) return [];
                return Array.from(list.shadowRoot.querySelectorAll('extensions-item')).map(i => {
                    const nameEl = i.shadowRoot && i.shadowRoot.querySelector('#name');
                    return {id: i.id, name: nameEl ? nameEl.textContent.trim() : ''};
                });
            }"""
        )
        for item in items or []:
            if "metamask" in (item.get("name") or "").lower():
                return item.get("id") or ""
    except Exception as exc:
        log.debug("读取 chrome://extensions 失败: %s", exc)
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass

    # 3) CDP target 标题兜底
    try:
        session = browser.new_browser_cdp_session()
        targets = session.send("Target.getTargets").get("targetInfos", [])
        for target in targets:
            match = EXT_RE.match(target.get("url", ""))
            if match and "metamask" in (target.get("title", "").lower()):
                return match.group(1)
    except Exception:
        pass
    return ""


# ------------------------------------------------------------------ UI 操作
def _visible(page: Page, selector: str, timeout_ms: int = 1500) -> bool:
    try:
        locator = page.locator(selector).first
        return locator.count() > 0 and locator.is_visible(timeout=timeout_ms)
    except Exception:
        return False


def _click(page: Page, selectors, timeout_s: int) -> bool:
    if isinstance(selectors, str):
        selectors = [selectors]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            locator.wait_for(state="visible", timeout=min(timeout_s * 1000, 8000))
            locator.click(timeout=timeout_s * 1000)
            return True
        except Exception:
            continue
    return False


def _click_text(page: Page, pattern: str, timeout_s: int) -> bool:
    try:
        locator = page.get_by_role("button", name=re.compile(pattern, re.I)).first
        if locator.count() == 0:
            return False
        locator.click(timeout=timeout_s * 1000)
        return True
    except Exception:
        return False


def _fill(page: Page, selector: str, value: str, timeout_s: int) -> bool:
    try:
        locator = page.locator(selector).first
        if locator.count() == 0:
            return False
        locator.wait_for(state="visible", timeout=min(timeout_s * 1000, 8000))
        locator.fill(value, timeout=timeout_s * 1000)
        return True
    except Exception:
        return False


def import_to_metamask(
    context: BrowserContext,
    extension_id: str,
    mnemonic: str,
    password: str,
    timeout_s: int = 20,
) -> Tuple[bool, str, Optional[Page]]:
    """把助记词导入 MetaMask，返回 (是否成功, 说明, 扩展页面)。"""
    page = context.new_page()
    base = f"chrome-extension://{extension_id}/home.html"
    try:
        page.goto(base, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        page.wait_for_timeout(2000)

        if _visible(page, SEL_UNLOCK) or _visible(page, SEL_HOME):
            return False, "该窗口已存在钱包", page

        # 未出现欢迎页时，直接尝试导入路由
        if not _visible(page, SEL_IMPORT_ENTRY, 3000) and not _visible(
            page, SEL_SRP_TEXTAREA, 3000
        ):
            try:
                page.goto(
                    base + "#/onboarding/import-with-srp",
                    wait_until="domcontentloaded",
                    timeout=timeout_s * 1000,
                )
                page.wait_for_timeout(1500)
            except Exception:
                pass

        # 欢迎页：同意条款 -> 导入已有钱包
        _click(page, '[data-testid="onboarding-terms-checkbox"]', timeout_s)
        if not _click(page, SEL_IMPORT_ENTRY, timeout_s):
            _click_text(
                page, r"^(Import an existing wallet|导入现有钱包|导入已有钱包)$", timeout_s
            )
        _click_text(page, r"^(I agree|我同意|同意)$", timeout_s)

        # 填助记词
        words = mnemonic.split()
        filled = False
        if _visible(page, SEL_SRP_TEXTAREA, 5000):
            filled = _fill(page, SEL_SRP_TEXTAREA, mnemonic, timeout_s)
        else:
            for index, word in enumerate(words):
                selector = SEL_SRP_WORD.format(index)
                if not _fill(page, selector, word, timeout_s):
                    filled = False
                    break
                filled = True
        if not filled:
            return False, "未找到助记词输入框（MetaMask 版本可能已变化）", page

        if not _click(page, SEL_SRP_CONFIRM, timeout_s):
            _click_text(page, r"^(Confirm|确认|导入)$", timeout_s)

        # 设置密码
        if not _fill(page, SEL_PWD_NEW, password, timeout_s):
            return False, "未出现设置密码页面", page
        _fill(page, SEL_PWD_CONFIRM, password, timeout_s)
        _click(page, SEL_PWD_TERMS, timeout_s)
        _click(page, SEL_PWD_SUBMIT, timeout_s)

        # 完成
        for _ in range(timeout_s * 2):
            if _visible(page, SEL_HOME, 300):
                return True, "导入成功", page
            for selector in SEL_READY:
                if _visible(page, selector, 300):
                    _click(page, [selector], 5)
                    page.wait_for_timeout(800)
                    return True, "导入成功", page
            if _visible(page, 'button:has-text("Done")', 300):
                _click_text(page, r"^(Done|完成|开始使用)$", 5)
                page.wait_for_timeout(800)
                return True, "导入成功", page
            page.wait_for_timeout(500)
        return False, "等待钱包创建完成超时", page
    except Exception as exc:
        return False, f"自动导入异常: {exc}", page


# ----------------------------------------------------------------- 人工兜底
def _prompt_manual() -> Tuple[str, str]:
    print("\n  请在浏览器窗口内手动完成 MetaMask 创建/导入。")
    try:
        address = input("  完成后输入钱包地址（可留空）: ").strip()
        mnemonic = input("  助记词（可留空，留空则只记录地址）: ").strip()
        return address, mnemonic
    except (EOFError, KeyboardInterrupt):
        return "", ""


def _take_screenshot(page: Optional[Page], directory: str, label: str) -> None:
    if page is None:
        return
    try:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w\-]+", "_", label)[:40]
        page.screenshot(path=str(path / f"wallet_fail_{safe}.png"))
    except Exception:
        pass


# ------------------------------------------------------------------ 对外入口
def ensure_wallet(
    browser: Browser,
    context: BrowserContext,
    cfg: WalletCfg,
    screenshot_dir: str,
    label: str = "",
) -> WalletResult:
    if not cfg.enabled or cfg.mode == "skip":
        return WalletResult("skip", note="已跳过钱包")

    if cfg.mode == "manual":
        address, mnemonic = _prompt_manual()
        return WalletResult("manual", address=address, mnemonic=mnemonic, note="人工创建")

    try:
        mnemonic, address, password = generate_wallet(
            cfg.password_prefix, cfg.password_length
        )
    except Exception as exc:
        return WalletResult("failed", note=str(exc))

    extension_id = find_extension_id(browser, context, cfg.extension_id)
    if not extension_id:
        return WalletResult(
            "failed", note="未检测到 MetaMask 扩展，请先在 ixBrowser 为该窗口安装 MetaMask"
        )

    ok, detail, page = import_to_metamask(
        context, extension_id, mnemonic, password, cfg.step_timeout
    )
    if ok:
        try:
            page.close()  # type: ignore[union-attr]
        except Exception:
            pass
        return WalletResult(
            "ok", address=address, mnemonic=mnemonic, password=password
        )

    if detail == "该窗口已存在钱包":
        try:
            page.close()  # type: ignore[union-attr]
        except Exception:
            pass
        return WalletResult("exists", note=detail)

    _take_screenshot(page, screenshot_dir, label)
    if cfg.manual_fallback:
        manual_address, manual_mnemonic = _prompt_manual()
        return WalletResult(
            "manual",
            address=manual_address or address,
            mnemonic=manual_mnemonic,
            password=password,
            note=f"自动导入失败（{detail}），已转人工",
        )
    return WalletResult("failed", note=detail)

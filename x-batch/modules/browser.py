"""ixBrowser 窗口调度：打开窗口 -> 拿调试地址 -> Playwright 接管。"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from .config import IxCfg

if TYPE_CHECKING:  # 仅用于类型提示，避免未安装 playwright 时无法启动
    from playwright.sync_api import Browser, BrowserContext, Playwright

log = logging.getLogger(__name__)


class IxBrowserError(RuntimeError):
    pass


class BrowserManager:
    def __init__(self, cfg: IxCfg):
        try:
            from ixbrowser_local_api import IXBrowserClient
        except ImportError as exc:  # pragma: no cover
            raise IxBrowserError(
                "缺少 ixbrowser-local-api，请先执行: pip install ixbrowser-local-api"
            ) from exc

        self._cfg = cfg
        try:
            # 1.3+ 的 SDK 支持 timeout 参数
            self._client = IXBrowserClient(
                target=cfg.host, port=cfg.port, timeout=(3, 30)
            )
        except TypeError:
            # 1.2.x 及更早版本没有 timeout 参数，只能传地址和端口
            self._client = IXBrowserClient(target=cfg.host, port=cfg.port)
        self._pw: Optional[Playwright] = None

    # ------------------------------------------------------------------ 窗口
    def list_profiles(self) -> List[Dict[str, str]]:
        """拉取全部窗口，返回 [{id, name}, ...]。"""
        profiles: List[Dict[str, Any]] = []
        page = 1
        while True:
            try:
                data = self._client.get_profile_list(page=page, limit=100)
            except TypeError:
                # 老版本 SDK 不支持分页参数
                data = self._client.get_profile_list()
                if data:
                    profiles.extend(data)
                break
            if data is None:
                raise IxBrowserError(
                    f"获取窗口列表失败: code={self._client.code} "
                    f"message={self._client.message}（请确认 ixBrowser 客户端已启动）"
                )
            profiles.extend(data)
            total = getattr(self._client, "total", 0) or 0
            if len(data) < 100 or (total and len(profiles) >= total):
                break
            page += 1

        result: List[Dict[str, str]] = []
        for item in profiles:
            pid = item.get("profile_id") or item.get("id") or ""
            if not pid:
                continue
            name = item.get("name") or item.get("profile_name") or ""
            result.append({"id": str(pid), "name": str(name)})
        return result

    def open(self, profile_id: str) -> str:
        """打开窗口，返回 http://ip:port 形式的 CDP 地址。"""
        data = self._client.open_profile(
            profile_id, cookies_backup=False, load_profile_info_page=False
        )
        if data is None:
            raise IxBrowserError(
                f"打开窗口 {profile_id} 失败: code={self._client.code} "
                f"message={self._client.message}"
            )
        address = data.get("debugging_address") or ""
        if not address:
            raise IxBrowserError(f"窗口 {profile_id} 未返回调试地址: {data}")
        if not address.startswith("http"):
            address = "http://" + address
        return address

    def close(self, profile_id: str) -> None:
        try:
            result = self._client.close_profile(profile_id)
            if result is None:
                log.warning(
                    "关闭窗口 %s 失败: %s", profile_id, self._client.message
                )
        except Exception as exc:  # 关闭失败不影响主流程
            log.warning("关闭窗口 %s 异常: %s", profile_id, exc)

    # ------------------------------------------------------------- Playwright
    def connect(self, url: str) -> Tuple["Browser", "BrowserContext"]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise IxBrowserError(
                "缺少 playwright，请先执行: pip install playwright"
            ) from exc
        if self._pw is None:
            self._pw = sync_playwright().start()
        browser = self._pw.chromium.connect_over_cdp(url, timeout=30_000)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        return browser, context

    def stop_playwright(self) -> None:
        """断开与当前窗口的 CDP 连接（不会关闭窗口本身）。"""
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None

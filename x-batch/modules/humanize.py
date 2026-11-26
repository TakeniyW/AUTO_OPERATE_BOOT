"""拟人化操作工具：随机等待、随机滚动、阅读停留。

所有等待都支持 stop_event，便于在 GUI 里随时停止。
"""
from __future__ import annotations

import logging
import random
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # 仅用于类型提示
    from playwright.sync_api import Page

log = logging.getLogger(__name__)


def chance(probability: float) -> bool:
    return random.random() < max(0.0, min(1.0, probability))


def sleep_interruptible(seconds: float, stop_event=None) -> bool:
    """可被 stop_event 打断的等待；返回 False 表示期间收到停止信号。"""
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        if stop_event is not None and stop_event.is_set():
            return False
        time.sleep(min(0.2, max(0.02, end - time.time())))
    return True


def rand_sleep(low: float, high: float, stop_event=None) -> bool:
    low, high = min(low, high), max(low, high)
    return sleep_interruptible(random.uniform(max(0.0, low), max(0.0, high)), stop_event)


def gauss_sleep(mean: float, sigma: float, low: float, high: float, stop_event=None) -> bool:
    """正态分布等待，比均匀分布更接近真人的操作节奏。"""
    target = max(low, min(high, random.gauss(mean, sigma)))
    return sleep_interruptible(target, stop_event)


def scroll_page(
    page: "Page",
    times: int = 1,
    small: bool = False,
    stop_event=None,
) -> bool:
    """随机幅度滚动，每次滚动后随机停顿。"""
    for _ in range(max(0, times)):
        try:
            distance = random.randint(220, 650) if small else random.randint(450, 1500)
            page.mouse.wheel(0, distance)
        except Exception:
            return False
        if not sleep_interruptible(random.uniform(0.35, 1.4), stop_event):
            return False
    return True


def read_pause(stop_event=None) -> bool:
    """阅读停留：多数在 3~10 秒，偶尔更久。"""
    return gauss_sleep(6.0, 3.5, 2.0, 18.0, stop_event)


def warm_browse(page: "Page", url: str, seconds: float, stop_event=None) -> bool:
    """登录前先以访客身份浏览一会儿，减少“开窗即登录”的机械特征。"""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as exc:
        log.debug("访客浏览打开失败: %s", exc)
        return False

    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        if stop_event is not None and stop_event.is_set():
            return False
        scroll_page(page, times=random.randint(1, 3), stop_event=stop_event)
        if not sleep_interruptible(random.uniform(1.2, 3.5), stop_event):
            return False
    return True

"""日志初始化：控制台 + 文件 + 可选的 GUI 队列。"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from queue import Queue
from typing import Optional

from .config import Settings

# GUI 队列里的结构化统计消息前缀：GUI 拦截前缀后不写日志，直接刷新统计区
STATS_PREFIX = "@@STATS "


def emit_stats(queue: Optional[Queue], payload: dict) -> None:
    """把结构化统计写入 GUI 队列（仅 GUI 模式传入 queue 时有效）。"""
    if queue is None:
        return
    try:
        queue.put(STATS_PREFIX + json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


class QueueLogHandler(logging.Handler):
    """把日志记录推到 Queue，GUI 线程从 Queue 读出来显示。"""

    def __init__(self, queue: Queue):
        super().__init__()
        self._q = queue
        self.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._q.put(self.format(record))
        except Exception:
            pass


def setup_logging(
    settings: Settings,
    gui_queue: Optional[Queue] = None,
    prefix: str = "run",
) -> None:
    log_dir = Path(settings.output.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}.log"

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if gui_queue is not None:
        root.addHandler(QueueLogHandler(gui_queue))

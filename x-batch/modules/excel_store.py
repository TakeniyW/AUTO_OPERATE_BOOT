"""Excel 读写：token 读取、状态回写、断点续跑。"""
from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from openpyxl import load_workbook

from .config import ExcelCfg

log = logging.getLogger(__name__)

STATUS_LOGINED = "已登录"
STATUS_INVALID = "失效"
STATUS_RESTRICTED = "受限/需人工"
STATUS_ERROR = "异常"
STATUS_MANUAL = "待人工"
STATUS_UNUSED = "未使用"

COL_STATUS = "状态"
COL_PROFILE_ID = "窗口ID"
COL_PROFILE_NAME = "窗口名称"
COL_USERNAME = "X用户名"
COL_ADDRESS = "钱包地址"
COL_MNEMONIC = "助记词"
COL_PASSWORD = "钱包密码"
COL_TIME = "处理时间"
COL_NOTE = "备注"

EXTRA_COLUMNS = [
    COL_STATUS,
    COL_PROFILE_ID,
    COL_PROFILE_NAME,
    COL_USERNAME,
    COL_ADDRESS,
    COL_MNEMONIC,
    COL_PASSWORD,
    COL_TIME,
    COL_NOTE,
]


@dataclass
class AccountRow:
    sheet_row: int
    token: str
    status: str = ""
    values: Dict[str, str] = field(default_factory=dict)


def _parse_token(raw) -> str:
    """兼容 auth_token=xxx / JSON / 裸值三种写法。"""
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return str(
                    data.get("auth_token") or data.get("authToken") or ""
                ).strip()
        except Exception:
            pass
    match = re.search(r"auth_token\s*=\s*([^;&\s\"']+)", text)
    if match:
        return match.group(1).strip()
    return text.strip("\"'")


class ExcelStore:
    def __init__(self, cfg: ExcelCfg):
        self.path = Path(cfg.path)
        if not self.path.exists():
            raise FileNotFoundError(f"Excel 不存在: {self.path.resolve()}")

        if cfg.backup:
            backup = self.path.with_suffix(self.path.suffix + ".bak")
            if not backup.exists():
                shutil.copy2(self.path, backup)
                log.info("已备份原表 -> %s", backup.name)

        self.wb = load_workbook(self.path)
        self.ws = self.wb[cfg.sheet] if cfg.sheet else self.wb.active

        self.header_row = self._find_header_row()
        self.columns: Dict[str, int] = {}
        for col in range(1, self.ws.max_column + 1):
            value = self.ws.cell(row=self.header_row, column=col).value
            if value is not None and str(value).strip():
                self.columns[str(value).strip()] = col

        self.token_col = self._resolve_token_column(cfg.token_column)
        self.ct0_col = self._optional_column(cfg.ct0_column)

        for name in EXTRA_COLUMNS:
            self._column(name)

        self.rows: List[AccountRow] = self._load_rows()

    # ------------------------------------------------------------- 列处理
    def _find_header_row(self) -> int:
        for row in range(1, min(10, self.ws.max_row) + 1):
            values = [
                self.ws.cell(row=row, column=c).value
                for c in range(1, min(self.ws.max_column, 20) + 1)
            ]
            if any(isinstance(v, str) and v.strip() for v in values):
                return row
        return 1

    def _resolve_token_column(self, configured: str) -> int:
        for name in (configured, "auth_token", "token"):
            if name and name in self.columns:
                return self.columns[name]
        for name, col in self.columns.items():
            if "token" in name.lower():
                return col
        raise RuntimeError(
            "找不到 token 列，请在 config.yaml 的 excel.token_column 里填写表头名。"
            f"当前表头: {list(self.columns)}"
        )

    def _optional_column(self, name: str) -> Optional[int]:
        if name and name in self.columns:
            return self.columns[name]
        return None

    def _column(self, name: str) -> int:
        if name in self.columns:
            return self.columns[name]
        index = self.ws.max_column + 1
        self.ws.cell(row=self.header_row, column=index, value=name)
        self.columns[name] = index
        return index

    # --------------------------------------------------------------- 读表
    def _load_rows(self) -> List[AccountRow]:
        rows: List[AccountRow] = []
        for sheet_row in range(self.header_row + 1, self.ws.max_row + 1):
            raw = self.ws.cell(row=sheet_row, column=self.token_col).value
            token = _parse_token(raw)
            if not token:
                continue
            status = str(
                self.ws.cell(row=sheet_row, column=self._column(COL_STATUS)).value
                or ""
            ).strip()
            rows.append(AccountRow(sheet_row=sheet_row, token=token, status=status))
        return rows

    def pending_rows(self) -> List[AccountRow]:
        """可处理的 token：未使用 / 空 / 上次异常中断。"""
        pending_status = {"", STATUS_UNUSED, STATUS_ERROR, "未处理"}
        return [row for row in self.rows if row.status in pending_status]

    # --------------------------------------------------------------- 写表
    def update(self, row: AccountRow, **fields) -> None:
        for name, value in fields.items():
            self.ws.cell(row=row.sheet_row, column=self._column(name), value=value)
            row.values[name] = "" if value is None else str(value)
            if name == COL_STATUS:
                row.status = str(value)
        self.ws.cell(
            row=row.sheet_row,
            column=self._column(COL_TIME),
            value=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    def ct0_of(self, row: AccountRow) -> str:
        if not self.ct0_col:
            return ""
        value = self.ws.cell(row=row.sheet_row, column=self.ct0_col).value
        return str(value).strip() if value else ""

    def save(self) -> None:
        try:
            self.wb.save(self.path)
        except PermissionError:
            alt = self.path.with_name(f"{self.path.stem}_results{self.path.suffix}")
            self.wb.save(alt)
            log.warning("原表被占用，结果已保存到: %s", alt.resolve())
            self.path = alt

    def summary(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for row in self.rows:
            key = row.status or "未处理"
            counts[key] = counts.get(key, 0) + 1
        return counts

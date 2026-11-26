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
STATUS_FAILED = "全部失败"

TOKEN_UNKNOWN = "未尝试"
TOKEN_OK = "成功"
TOKEN_FAILED = "失效"
PASSWORD_UNKNOWN = "未尝试"
PASSWORD_OK = "成功"
PASSWORD_FAILED = "失败"

COL_STATUS = "状态"
COL_TOKEN_LOGIN = "token登录"
COL_PASSWORD_LOGIN = "账号登录"
COL_FINAL = "最终结果"
COL_PROFILE_ID = "窗口ID"
COL_PROFILE_NAME = "窗口名称"
COL_USERNAME = "X用户名"
COL_ADDRESS = "钱包地址"
COL_MNEMONIC = "助记词"
COL_PASSWORD = "钱包密码"
COL_TIME = "处理时间"
COL_NOTE = "备注"
COL_WARMUP_COUNT = "养号次数"
COL_WARMUP_TIME = "最后养号时间"

EXTRA_COLUMNS = [
    COL_STATUS,
    COL_TOKEN_LOGIN,
    COL_PASSWORD_LOGIN,
    COL_FINAL,
    COL_PROFILE_ID,
    COL_PROFILE_NAME,
    COL_USERNAME,
    COL_ADDRESS,
    COL_MNEMONIC,
    COL_PASSWORD,
    COL_TIME,
    COL_NOTE,
    COL_WARMUP_COUNT,
    COL_WARMUP_TIME,
]


@dataclass
class AccountRow:
    sheet_row: int
    token: str
    idx: str = ""
    account: str = ""
    password: str = ""
    twofa: str = ""
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
        self.idx_col = self._optional_column("序号")
        self.account_col = self._optional_column(cfg.username_column)
        self.password_col = self._optional_column(cfg.password_column)
        self.twofa_col = self._optional_column(cfg.twofa_column)

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
    def _cell_text(self, row: int, col: Optional[int]) -> str:
        if not col:
            return ""
        value = self.ws.cell(row=row, column=col).value
        return str(value).strip() if value is not None else ""

    def _load_rows(self) -> List[AccountRow]:
        rows: List[AccountRow] = []
        for sheet_row in range(self.header_row + 1, self.ws.max_row + 1):
            raw = self.ws.cell(row=sheet_row, column=self.token_col).value
            token = _parse_token(raw)
            account = self._cell_text(sheet_row, self.account_col)
            # token 和账号都为空才跳过（允许 token 失效但账号密码可用的行）
            if not token and not account:
                continue
            status = self._cell_text(sheet_row, self._column(COL_STATUS))
            rows.append(
                AccountRow(
                    sheet_row=sheet_row,
                    token=token,
                    idx=self._cell_text(sheet_row, self.idx_col),
                    account=account,
                    password=self._cell_text(sheet_row, self.password_col),
                    twofa=self._cell_text(sheet_row, self.twofa_col),
                    status=status,
                )
            )
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

    def value_of(self, row: AccountRow, name: str) -> str:
        """读取任意列的值（列不存在时返回空字符串）。"""
        return self._cell_text(row.sheet_row, self._optional_column(name))

    def bump_warmup(self, row: AccountRow) -> int:
        """养号次数 +1 并记录时间，返回新的次数。"""
        raw = self.value_of(row, COL_WARMUP_COUNT)
        try:
            count = int(float(raw))
        except (TypeError, ValueError):
            count = 0
        count += 1
        self.update(
            row,
            **{
                COL_WARMUP_COUNT: count,
                COL_WARMUP_TIME: datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        return count

    def is_write_locked(self) -> bool:
        """检测原表是否被 Excel/WPS 占用（占用时无法写回）。"""
        try:
            with open(self.path, "ab"):
                return False
        except PermissionError:
            return True
        except OSError:
            return False

    def save(self) -> None:
        try:
            self.wb.save(self.path)
        except PermissionError:
            alt = self.path.with_name(f"{self.path.stem}_results{self.path.suffix}")
            self.wb.save(alt)
            log.warning(
                "原表被 Excel/WPS 占用，本次结果写入: %s（关闭表格后重跑即可写回原表）",
                alt.resolve(),
            )
            self.path = alt

    def summary(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for row in self.rows:
            key = row.status or "未处理"
            counts[key] = counts.get(key, 0) + 1
        return counts


# ---------------------------------------------------------- 模版 / 结果导出
def create_template(
    path: str,
    *,
    token_column: str = "auth_token",
    ct0_column: str = "",
    username_column: str = "账号",
    password_column: str = "密码",
    twofa_column: str = "2FA",
) -> Path:
    """生成默认格式的登录表：只有表头 + 一页填写说明，用户直接填数据即可。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    columns = [("序号", 8)]
    columns.append((token_column or "auth_token", 64))
    if ct0_column:
        columns.append((ct0_column, 40))
    columns.append((username_column or "账号", 22))
    columns.append((password_column or "密码", 20))
    columns.append((twofa_column or "2FA", 56))

    wb = Workbook()
    ws = wb.active
    ws.title = "登录"
    ws.append([name for name, _ in columns])
    fill = PatternFill("solid", fgColor="2F5597")
    for idx, (_, width) in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=idx)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.row_dimensions[1].height = 22
    ws.freeze_panes = "A2"

    tips = wb.create_sheet("填写说明")
    tips.column_dimensions["A"].width = 100
    lines = [
        "填写说明（本页不参与处理，可删除）",
        "",
        "1. 「登录」页第 1 行是表头，请勿修改列名；数据从第 2 行开始逐行填写。",
        f"2. {token_column or 'auth_token'}：Twitter auth_token，支持 auth_token=xxx、JSON、裸值三种写法。",
        f"3. {username_column or '账号'} / {password_column or '密码'}：账号密码登录使用；不启用时可留空。",
        f"4. {twofa_column or '2FA'}：2FA 验证链接或密钥，形如 https://2fa.xdgongju.top/?key=XXXX。",
        "5. 运行前请关闭 Excel / WPS 中打开的该表，否则结果无法写回原表。",
        "6. 运行结束后可在「① Excel」点「下载执行结果 Excel」另存副本，原表保持不变。",
    ]
    for row, text in enumerate(lines, start=1):
        tips.cell(row=row, column=1, value=text)
    tips["A1"].font = Font(bold=True, size=12)

    dest = Path(path)
    if not dest.suffix:
        dest = dest.with_suffix(".xlsx")
    dest.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dest)
    log.info("已生成登录模版: %s", dest.resolve())
    return dest


def resolve_result_path(configured: str) -> Path:
    """返回最近写入的结果表（原表或被占用时生成的 *_results 副本）。"""
    base = Path(configured)
    if not base.suffix:
        base = base.with_suffix(".xlsx")
    candidates = [base, base.with_name(f"{base.stem}_results{base.suffix}")]
    existing = [p for p in candidates if p.is_file()]
    if not existing:
        raise FileNotFoundError(f"找不到结果表: {base}")
    return max(existing, key=lambda p: p.stat().st_mtime)


def export_copy(source: str, dest: str) -> Path:
    """把结果表另存为新文件；目标与原表相同时拒绝，确保不改动原表。"""
    src = Path(source)
    dst = Path(dest)
    if not src.is_file():
        raise FileNotFoundError(f"找不到结果表: {src}")
    if not dst.suffix:
        dst = dst.with_suffix(src.suffix or ".xlsx")
    if src.resolve() == dst.resolve():
        raise ValueError("另存路径不能与原表相同，请换一个文件名")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    log.info("结果表已另存: %s -> %s", src, dst)
    return dst

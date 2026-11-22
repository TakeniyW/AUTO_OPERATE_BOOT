"""本地状态库（SQLite）。

每条 Excel 记录对应一行，分别记录：
- token 登录结果
- 账号密码登录结果
- 最终结果
GUI 的“记录查询”页签直接读这个库，不依赖 Excel 是否被占用。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List

TOKEN_UNKNOWN = "未尝试"
PASSWORD_UNKNOWN = "未尝试"
FINAL_PENDING = "未处理"

_ALLOWED = {
    "idx",
    "account",
    "token_status",
    "password_status",
    "final_status",
    "profile",
    "username",
    "note",
}


class StateDB:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    sheet_row INTEGER PRIMARY KEY,
                    idx TEXT,
                    account TEXT,
                    token_status TEXT,
                    password_status TEXT,
                    final_status TEXT,
                    profile TEXT,
                    username TEXT,
                    note TEXT,
                    updated_at TEXT
                )
                """
            )

    def upsert(self, sheet_row: int, **fields) -> None:
        data = {k: v for k, v in fields.items() if k in _ALLOWED and v is not None}
        data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM records WHERE sheet_row = ?", (sheet_row,)
            ).fetchone()
            if exists:
                assignments = ", ".join(f"{name} = ?" for name in data)
                conn.execute(
                    f"UPDATE records SET {assignments} WHERE sheet_row = ?",
                    (*data.values(), sheet_row),
                )
            else:
                columns = ["sheet_row", *data]
                placeholders = ", ".join("?" for _ in columns)
                conn.execute(
                    f"INSERT INTO records ({', '.join(columns)}) VALUES ({placeholders})",
                    (sheet_row, *data.values()),
                )

    def query(self, keyword: str = "", status: str = "") -> List[Dict[str, str]]:
        sql = "SELECT * FROM records"
        conditions: List[str] = []
        args: List[str] = []
        keyword = (keyword or "").strip()
        if keyword:
            like = f"%{keyword}%"
            conditions.append(
                "(account LIKE ? OR username LIKE ? OR idx LIKE ? OR profile LIKE ? OR note LIKE ?)"
            )
            args.extend([like] * 5)
        if status:
            conditions.append("final_status = ?")
            args.append(status)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY sheet_row"
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]

    def summary(self) -> Dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT final_status, COUNT(*) AS c FROM records GROUP BY final_status"
            ).fetchall()
        return {(row["final_status"] or FINAL_PENDING): row["c"] for row in rows}

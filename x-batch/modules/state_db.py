"""本地状态库（SQLite）。

每条 Excel 记录对应一行，分别记录：
- token 登录结果
- 账号密码登录结果
- 最终结果
GUI 的“记录查询”页签直接读这个库，不依赖 Excel 是否被占用。
"""
from __future__ import annotations

import sqlite3
import threading
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
        # 每个线程复用自己的连接，避免多线程共用一个 sqlite 连接
        self._local = threading.local()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
        except Exception:
            pass
        self._local.conn = conn
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS warmup_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT,
                    account TEXT,
                    profile TEXT,
                    actions INTEGER DEFAULT 0,
                    likes INTEGER DEFAULT 0,
                    retweets INTEGER DEFAULT 0,
                    bookmarks INTEGER DEFAULT 0,
                    follows INTEGER DEFAULT 0,
                    seconds INTEGER DEFAULT 0,
                    note TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_warmup_day_account "
                "ON warmup_log(day, account)"
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

    def query(
        self,
        keyword: str = "",
        status: str = "",
        date_from: str = "",
        date_to: str = "",
    ) -> List[Dict[str, str]]:
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
        date_from = (date_from or "").strip()
        date_to = (date_to or "").strip()
        if date_from:
            conditions.append("updated_at >= ?")
            args.append(f"{date_from} 00:00:00")
        if date_to:
            conditions.append("updated_at <= ?")
            args.append(f"{date_to} 23:59:59")
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

    # ------------------------------------------------------------ 养号记录
    @staticmethod
    def today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def warmup_add(
        self,
        account: str,
        profile: str = "",
        actions: int = 0,
        likes: int = 0,
        retweets: int = 0,
        bookmarks: int = 0,
        follows: int = 0,
        seconds: int = 0,
        note: str = "",
        day: str = "",
    ) -> None:
        day = day or self.today()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO warmup_log (day, account, profile, actions, likes, "
                "retweets, bookmarks, follows, seconds, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    day,
                    account,
                    profile,
                    actions,
                    likes,
                    retweets,
                    bookmarks,
                    follows,
                    seconds,
                    note,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )

    def warmup_count_today(self, account: str, day: str = "") -> int:
        day = day or self.today()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM warmup_log WHERE day = ? AND account = ?",
                (day, account),
            ).fetchone()
        return int(row["c"]) if row else 0

    def warmup_last(self, account: str) -> Dict[str, str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM warmup_log WHERE account = ? ORDER BY id DESC LIMIT 1",
                (account,),
            ).fetchone()
        return dict(row) if row else {}

    def warmup_summary_today(self, day: str = "") -> Dict[str, int]:
        day = day or self.today()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS accounts, "
                "COALESCE(SUM(actions), 0) AS actions, "
                "COALESCE(SUM(likes), 0) AS likes, "
                "COALESCE(SUM(retweets), 0) AS retweets, "
                "COALESCE(SUM(follows), 0) AS follows "
                "FROM warmup_log WHERE day = ?",
                (day,),
            ).fetchone()
        return {k: int(row[k]) for k in ("accounts", "actions", "likes", "retweets", "follows")} if row else {}


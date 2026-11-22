"""配置加载：把 config.yaml 转成简单的配置对象，缺省值兜底。"""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Dict, Type, TypeVar

import yaml

T = TypeVar("T")


def _build(cls: Type[T], raw: Dict[str, Any]) -> T:
    """只接收 dataclass 已声明的字段，忽略多余键，缺失的用默认值。"""
    allowed = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (raw or {}).items() if k in allowed})


@dataclass
class ExcelCfg:
    path: str = "accounts.xlsx"
    sheet: str = ""
    token_column: str = "auth_token"
    ct0_column: str = ""
    username_column: str = "账号"
    password_column: str = "密码"
    twofa_column: str = "2FA"
    backup: bool = True


@dataclass
class IxCfg:
    host: str = "127.0.0.1"
    port: int = 53200
    profile_name_filter: str = ""
    profile_limit: int = 0          # 0 = 不限制
    keep_window_open: bool = False  # 调试时可保留窗口


@dataclass
class TwitterCfg:
    home_url: str = "https://x.com/home"
    login_url: str = "https://x.com/i/flow/login"
    login_timeout: int = 25          # 单次判定超时（秒）
    password_login: bool = True      # token 失败后是否用账号密码兜底
    password_timeout: int = 40       # 账号密码登录总超时（秒）
    max_tokens_per_profile: int = 20 # 单个窗口最多尝试的 token 数
    retry_per_token: int = 1         # 网络类错误的重试次数
    delay_min: float = 3.0
    delay_max: float = 6.0


@dataclass
class WalletCfg:
    enabled: bool = True
    mode: str = "auto"               # auto / manual / skip
    extension_id: str = ""
    password_prefix: str = "Wx"
    password_length: int = 14
    step_timeout: int = 20
    manual_fallback: bool = True


@dataclass
class OutputCfg:
    log_dir: str = "logs"
    screenshot_dir: str = "screenshots"
    state_db: str = "data/state.db"


@dataclass
class Settings:
    excel: ExcelCfg
    ixbrowser: IxCfg
    twitter: TwitterCfg
    wallet: WalletCfg
    output: OutputCfg


def load_settings(path: str) -> Settings:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {p.resolve()}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return Settings(
        excel=_build(ExcelCfg, raw.get("excel")),
        ixbrowser=_build(IxCfg, raw.get("ixbrowser")),
        twitter=_build(TwitterCfg, raw.get("twitter")),
        wallet=_build(WalletCfg, raw.get("wallet")),
        output=_build(OutputCfg, raw.get("output")),
    )

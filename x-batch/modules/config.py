"""配置加载：把 config.yaml 转成简单的配置对象，缺省值兜底。"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Type, TypeVar

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
    profile_start: int = 1          # 起始序号（1 开始，按窗口列表顺序）
    profile_end: int = 0            # 结束序号，0 = 到最后一个
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
class RandomizeCfg:
    """行为随机化：打乱顺序、随机间隔、分批休息，降低批量特征。"""

    shuffle_rows: bool = True         # 打乱待处理账号顺序
    shuffle_profiles: bool = False    # 打乱窗口顺序（手动指定序号时建议关闭）
    account_delay_min: float = 20.0   # 两个账号之间的随机等待（秒）
    account_delay_max: float = 90.0
    batch_size: int = 0               # 每处理 N 个账号后长休息，0 = 不休息
    batch_rest_min: float = 300.0     # 长休息时间（秒）
    batch_rest_max: float = 900.0
    daily_limit: int = 0              # 本次最多处理多少条记录，0 = 不限
    warm_before_login: bool = False   # 注入 token 前先以访客身份浏览一会儿
    warm_seconds_min: int = 5
    warm_seconds_max: int = 15


@dataclass
class WarmupCfg:
    """推特养号：随机浏览、点赞、转推、收藏、关注、搜索。"""

    enabled: bool = True
    login_mode: str = "auto"          # auto = 有 token 用 token；session = 只用窗口已有登录态
    session_min: int = 120            # 单账号本次停留时长（秒）
    session_max: int = 300
    scroll_min: float = 2.0           # 每次滚动后的停留（秒）
    scroll_max: float = 7.0
    max_scrolls: int = 20             # 单次会话最多滚动次数
    like_prob: float = 0.25           # 每条候选推文的点赞概率
    max_likes: int = 6
    retweet_prob: float = 0.03
    max_retweets: int = 2
    bookmark_prob: float = 0.05       # 收藏概率
    max_bookmarks: int = 2
    follow_prob: float = 0.04
    max_follows: int = 1
    video_prob: float = 0.12          # 打开视频并观看的概率
    search_prob: float = 0.30         # 本次会话是否去搜索页浏览
    profile_prob: float = 0.25        # 本次会话是否看一眼自己的主页
    keywords: List[str] = field(default_factory=list)  # 搜索关键词，空则跳过搜索
    accounts_per_run: int = 0         # 本次最多养几个号，0 = 全部符合条件的
    daily_per_account: int = 1        # 每个号每天最多养几次
    active_hours: str = ""            # 允许运行时段，如 "9-23"；空 = 不限
    rest_min: int = 60                # 两个号之间的休息（秒）
    rest_max: int = 240


@dataclass
class Settings:
    excel: ExcelCfg
    ixbrowser: IxCfg
    twitter: TwitterCfg
    wallet: WalletCfg
    output: OutputCfg
    randomize: RandomizeCfg = field(default_factory=RandomizeCfg)
    warmup: WarmupCfg = field(default_factory=WarmupCfg)


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
        randomize=_build(RandomizeCfg, raw.get("randomize")),
        warmup=_build(WarmupCfg, raw.get("warmup")),
    )

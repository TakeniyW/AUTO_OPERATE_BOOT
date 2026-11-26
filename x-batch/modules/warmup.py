"""推特养号：在 ixBrowser 窗口里模拟真人浏览与轻量互动。

动作全部随机化：滚动时间线、打开推文阅读、点赞、转推、收藏、看视频、
侧边栏关注、按关键词搜索。每个号每天只跑有限次数，账号之间随机休息。

用法:
    python main.py --warmup                 # 使用 config.yaml 的 warmup 段
    python main.py --warmup --warmup-accounts 3
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional
from urllib.parse import quote

from . import humanize, tw_login
from .applog import setup_logging
from .browser import BrowserManager
from .config import Settings, WarmupCfg
from .excel_store import (
    COL_PROFILE_ID,
    COL_PROFILE_NAME,
    COL_STATUS,
    STATUS_LOGINED,
    STATUS_RESTRICTED,
    STATUS_UNUSED,
    AccountRow,
    ExcelStore,
)
from .state_db import StateDB

if TYPE_CHECKING:
    from playwright.sync_api import Page

log = logging.getLogger("x-warmup")

TWEET = 'article[data-testid="tweet"]'
TWEET_TIME_LINK = 'a[href*="/status/"] time'
LIKE = '[data-testid="like"]'
UNLIKE = '[data-testid="unlike"]'
RETWEET = '[data-testid="retweet"]'
UNRETWEET = '[data-testid="unretweet"]'
RETWEET_CONFIRM = '[data-testid="retweetConfirm"]'
BOOKMARK = '[data-testid="bookmark"]'
BOOKMARK_DONE = '[data-testid="removeBookmark"]'
FOLLOW = '[data-testid$="-follow"]'
SIDEBAR_USER = '[data-testid="UserCell"]'

HOME_URL = "https://x.com/home"
SEARCH_URL = "https://x.com/search?q={kw}&src=typed_query&f=live"


@dataclass
class SessionStats:
    scrolls: int = 0
    actions: int = 0
    likes: int = 0
    retweets: int = 0
    bookmarks: int = 0
    follows: int = 0
    videos: int = 0
    searches: int = 0
    seconds: int = 0

    def summary(self) -> str:
        return (
            f"滚动 {self.scrolls} 次 / 点赞 {self.likes} / 转推 {self.retweets} / "
            f"收藏 {self.bookmarks} / 关注 {self.follows} / 视频 {self.videos} / "
            f"搜索 {self.searches}，用时 {self.seconds}s"
        )


# ------------------------------------------------------------------ 页面工具
def _logged_out(page: "Page") -> bool:
    try:
        url = (page.url or "").lower()
    except Exception:
        return True
    return "/i/flow/login" in url or "/login" in url


def _goto(page: "Page", url: str, wait: float = 2.5) -> bool:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as exc:
        log.debug("打开页面失败 %s: %s", url, exc)
        return False
    if _logged_out(page):
        return False
    time.sleep(wait)
    return True


def _visible_tweets(page: "Page", limit: int = 20) -> List:
    result = []
    try:
        items = page.locator(TWEET)
        count = min(items.count(), limit)
    except Exception:
        return result
    for index in range(count):
        try:
            item = items.nth(index)
            if item.is_visible():
                result.append(item)
        except Exception:
            continue
    return result


def _click(locator, timeout: int = 4000) -> bool:
    try:
        if locator.count() == 0:
            return False
        locator.first.click(timeout=timeout)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ 单个动作
def _scroll_timeline(page: "Page", stats: SessionStats, cfg: WarmupCfg, stop_event) -> bool:
    times = random.randint(1, 3)
    if not humanize.scroll_page(page, times=times, stop_event=stop_event):
        return False
    stats.scrolls += times
    stats.actions += 1
    return humanize.rand_sleep(cfg.scroll_min, cfg.scroll_max, stop_event)


def _read_tweet(page: "Page", stats: SessionStats, stop_event) -> bool:
    tweets = _visible_tweets(page)
    random.shuffle(tweets)
    for tweet in tweets[:6]:
        try:
            link = tweet.locator(TWEET_TIME_LINK).first
            if link.count() == 0:
                continue
            link.click(timeout=4000)
            break
        except Exception:
            continue
    else:
        return False

    if not humanize.read_pause(stop_event):
        return False
    # 详情页里再随机停留一会儿，然后返回
    humanize.scroll_page(page, times=random.randint(1, 2), small=True, stop_event=stop_event)
    try:
        page.go_back(wait_until="domcontentloaded", timeout=15_000)
    except Exception:
        _goto(page, HOME_URL)
    stats.actions += 1
    return True


def _like_random(page: "Page", stats: SessionStats, cfg: WarmupCfg, stop_event) -> bool:
    if stats.likes >= cfg.max_likes:
        return False
    tweets = _visible_tweets(page)
    random.shuffle(tweets)
    for tweet in tweets[:8]:
        try:
            if tweet.locator(UNLIKE).count() > 0:
                continue  # 已经点过赞
            if _click(tweet.locator(LIKE)):
                stats.likes += 1
                stats.actions += 1
                return humanize.rand_sleep(1.5, 4.5, stop_event)
        except Exception:
            continue
    return False


def _retweet_random(page: "Page", stats: SessionStats, cfg: WarmupCfg, stop_event) -> bool:
    if stats.retweets >= cfg.max_retweets:
        return False
    tweets = _visible_tweets(page)
    random.shuffle(tweets)
    for tweet in tweets[:8]:
        try:
            if tweet.locator(UNRETWEET).count() > 0:
                continue  # 已转推，避免误点撤销
            if not _click(tweet.locator(RETWEET)):
                continue
            time.sleep(random.uniform(0.8, 1.8))
            if _click(page.locator(RETWEET_CONFIRM)):
                stats.retweets += 1
                stats.actions += 1
                return humanize.rand_sleep(2.0, 5.0, stop_event)
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
        except Exception:
            continue
    return False


def _bookmark_random(page: "Page", stats: SessionStats, cfg: WarmupCfg, stop_event) -> bool:
    tweets = _visible_tweets(page)
    random.shuffle(tweets)
    for tweet in tweets[:8]:
        try:
            if tweet.locator(BOOKMARK_DONE).count() > 0:
                continue
            if _click(tweet.locator(BOOKMARK)):
                stats.bookmarks += 1
                stats.actions += 1
                return humanize.rand_sleep(1.5, 4.0, stop_event)
        except Exception:
            continue
    return False


def _watch_video(page: "Page", stats: SessionStats, stop_event) -> bool:
    try:
        videos = page.locator("video")
        if videos.count() == 0:
            return False
        video = videos.first
        if not video.is_visible():
            return False
        video.click(timeout=4000)
    except Exception:
        return False
    stats.videos += 1
    stats.actions += 1
    return humanize.rand_sleep(5, 18, stop_event)


def _follow_someone(page: "Page", stats: SessionStats, cfg: WarmupCfg, stop_event) -> bool:
    if stats.follows >= cfg.max_follows:
        return False
    try:
        cells = page.locator(SIDEBAR_USER)
        count = min(cells.count(), 10)
    except Exception:
        return False
    indexes = list(range(count))
    random.shuffle(indexes)
    for index in indexes:
        try:
            button = cells.nth(index).locator(FOLLOW).first
            if button.count() == 0:
                continue
            if _click(button):
                stats.follows += 1
                stats.actions += 1
                return humanize.rand_sleep(2.0, 5.0, stop_event)
        except Exception:
            continue
    return False


def _search_keyword(
    page: "Page", stats: SessionStats, cfg: WarmupCfg, stop_event
) -> bool:
    if not cfg.keywords:
        return False
    keyword = random.choice(cfg.keywords)
    log.info("    搜索关键词: %s", keyword)
    if not _goto(page, SEARCH_URL.format(kw=quote(keyword))):
        return False
    stats.searches += 1
    stats.actions += 1
    if not humanize.rand_sleep(2.0, 5.0, stop_event):
        return False
    humanize.scroll_page(page, times=random.randint(2, 4), stop_event=stop_event)
    stats.scrolls += 3
    # 搜索页里随机点赞一条
    if humanize.chance(cfg.like_prob) and stats.likes < cfg.max_likes:
        _like_random(page, stats, cfg, stop_event)
    return _goto(page, HOME_URL)


def _visit_profile(page: "Page", handle: str, stats: SessionStats, stop_event) -> bool:
    if not handle:
        return False
    url = f"https://x.com/{handle.lstrip('@')}"
    if not _goto(page, url):
        return False
    stats.actions += 1
    humanize.scroll_page(page, times=random.randint(2, 4), stop_event=stop_event)
    stats.scrolls += 3
    if not humanize.read_pause(stop_event):
        return False
    return _goto(page, HOME_URL)


def _pick_action(
    cfg: WarmupCfg, stats: SessionStats, state: Dict[str, bool], has_handle: bool
) -> str:
    choices = [("scroll", 50), ("read", 14)]
    if has_handle and not state["profile"]:
        choices.append(("profile", 6))
    if stats.likes < cfg.max_likes:
        choices.append(("like", int(cfg.like_prob * 100)))
    if stats.retweets < cfg.max_retweets:
        choices.append(("retweet", int(cfg.retweet_prob * 100)))
    if stats.bookmarks < cfg.max_bookmarks:
        choices.append(("bookmark", int(cfg.bookmark_prob * 100)))
    if stats.follows < cfg.max_follows:
        choices.append(("follow", int(cfg.follow_prob * 100)))
    if not state["video"]:
        choices.append(("video", int(cfg.video_prob * 100)))
    if cfg.keywords and not state["search"]:
        choices.append(("search", int(cfg.search_prob * 100)))

    names = [name for name, _ in choices]
    weights = [max(1, weight) for _, weight in choices]
    return random.choices(names, weights=weights, k=1)[0]


# ------------------------------------------------------------------ 会话
def _run_session(page: "Page", cfg: WarmupCfg, handle: str, stop_event) -> SessionStats:
    stats = SessionStats()
    low, high = sorted((max(10, cfg.session_min), max(10, cfg.session_max)))
    duration = random.randint(low, high)
    log.info("  本次养号时长 %ds（@%s）", duration, handle or "unknown")

    if not _goto(page, HOME_URL):
        log.warning("  打开主页失败或未登录")
        return stats

    if not humanize.read_pause(stop_event):
        return stats

    deadline = time.time() + duration
    state = {"search": False, "video": False, "profile": False}
    steps = 0
    max_steps = max(20, cfg.max_scrolls * 2 + 10)

    while time.time() < deadline and steps < max_steps:
        if stop_event is not None and stop_event.is_set():
            break
        steps += 1

        action = _pick_action(cfg, stats, state, bool(handle))
        log.info("    动作: %s", action)

        if action == "scroll":
            ok = _scroll_timeline(page, stats, cfg, stop_event)
        elif action == "read":
            ok = _read_tweet(page, stats, stop_event)
        elif action == "like":
            ok = _like_random(page, stats, cfg, stop_event)
        elif action == "retweet":
            ok = _retweet_random(page, stats, cfg, stop_event)
        elif action == "bookmark":
            ok = _bookmark_random(page, stats, cfg, stop_event)
        elif action == "follow":
            ok = _follow_someone(page, stats, cfg, stop_event)
        elif action == "video":
            state["video"] = True
            ok = _watch_video(page, stats, stop_event)
        elif action == "search":
            state["search"] = True
            ok = _search_keyword(page, stats, cfg, stop_event)
        elif action == "profile":
            state["profile"] = True
            ok = _visit_profile(page, handle, stats, stop_event)
        else:
            ok = False

        if not ok:
            humanize.rand_sleep(1.0, 3.0, stop_event)

        humanize.rand_sleep(1.5, 6.0, stop_event)

    stats.seconds = int(duration - max(0, deadline - time.time()))
    return stats


# ------------------------------------------------------------------ 账号调度
def _account_key(row: AccountRow) -> str:
    return row.account or row.idx or f"row{row.sheet_row}"


def _in_active_hours(spec: str) -> bool:
    spec = (spec or "").strip()
    if not spec:
        return True
    try:
        left, right = spec.split("-")
        begin, end = int(left), int(right)
    except Exception:
        log.warning("active_hours 格式不正确（示例 9-23），本次忽略该限制")
        return True
    hour = datetime.now().hour
    if begin <= end:
        return begin <= hour < end
    return hour >= begin or hour < end


def _pick_profile(
    row: AccountRow,
    store: ExcelStore,
    id_to_name: Dict[str, str],
    used: set,
    free_profiles: List[Dict[str, str]],
    reserved: Dict[str, str],
) -> Optional[Dict[str, str]]:
    # 已绑定窗口的账号只用自己的窗口，保证 IP 不串；被占用则本次跳过
    bound = store.value_of(row, COL_PROFILE_ID)
    if bound:
        if bound not in id_to_name or bound in used:
            return None
        return {"id": bound, "name": id_to_name[bound]}

    # 未绑定的账号挑一个既没用过、也没被其他账号预留的窗口
    for profile in free_profiles:
        if profile["id"] in used:
            continue
        if profile["id"] in reserved and reserved[profile["id"]] != _account_key(row):
            continue
        return profile
    return None


def _login(
    context,
    row: AccountRow,
    settings: Settings,
    store: ExcelStore,
) -> tw_login.LoginResult:
    # 先看窗口里是否已经有登录态，避免为了重登把好会话清掉
    result = tw_login.check_session(
        context, home_url=settings.twitter.home_url, timeout=12
    )
    if result.status in (tw_login.OK, tw_login.RESTRICTED):
        return result

    mode = (settings.warmup.login_mode or "auto").strip().lower()
    if mode != "session" and row.token:
        result = tw_login.login_with_token(
            context,
            row.token,
            home_url=settings.twitter.home_url,
            timeout=settings.twitter.login_timeout,
            ct0=store.ct0_of(row),
        )
        if result.status == tw_login.OK:
            return result
        log.warning("  token 登录失败: %s", result.detail)

    return result


def _process_account(
    bm: BrowserManager,
    profile: Dict[str, str],
    row: AccountRow,
    settings: Settings,
    store: ExcelStore,
    db: StateDB,
    stop_event,
) -> Optional[SessionStats]:
    cfg = settings.warmup
    account = _account_key(row)
    try:
        debugging = bm.open(profile["id"])
        time.sleep(1.0)
        browser, context = bm.connect(debugging)
    except Exception as exc:
        log.error("  打开窗口失败: %s", exc)
        bm.stop_playwright()
        return None

    try:
        login = _login(context, row, settings, store)
        if login.status != tw_login.OK:
            log.warning("  跳过 %s：%s", account, login.detail or login.status)
            if login.status == tw_login.RESTRICTED:
                store.update(row, **{COL_STATUS: STATUS_RESTRICTED, "备注": "养号时发现账号受限"})
                store.save()
            return None

        handle = login.username.lstrip("@") if login.username else ""
        log.info("  登录成功: @%s，开始养号", handle or account)
        pages = [p for p in context.pages if not p.is_closed()]
        page = pages[0] if pages else context.new_page()
        stats = _run_session(page, cfg, handle, stop_event)

        db.warmup_add(
            account=account,
            profile=profile["name"] or profile["id"],
            actions=stats.actions,
            likes=stats.likes,
            retweets=stats.retweets,
            bookmarks=stats.bookmarks,
            follows=stats.follows,
            seconds=stats.seconds,
            note=stats.summary(),
        )
        count = store.bump_warmup(row)
        store.update(
            row,
            **{
                COL_PROFILE_ID: profile["id"],
                COL_PROFILE_NAME: profile["name"],
                "备注": f"养号第 {count} 次: {stats.summary()}",
            },
        )
        store.save()

        log.info(
            "  养号完成 @%s: %s（累计第 %d 次）",
            handle or account,
            stats.summary(),
            count,
        )
        return stats
    finally:
        bm.stop_playwright()
        if not settings.ixbrowser.keep_window_open:
            try:
                bm.close(profile["id"])
            except Exception:
                pass


def run(
    settings: Settings,
    gui_queue=None,
    stop_event=None,
) -> int:
    """养号入口：与 main.run 相同的调用签名，GUI / CLI 共用。"""
    setup_logging(settings, gui_queue, prefix="warmup")
    cfg = settings.warmup

    if not cfg.enabled:
        log.info("养号功能已关闭（warmup.enabled = false），退出")
        return 0
    if not _in_active_hours(cfg.active_hours):
        log.info("当前不在允许时段（%s），退出", cfg.active_hours)
        return 0

    try:
        store = ExcelStore(settings.excel)
    except Exception as exc:
        log.error("读取 Excel 失败: %s", exc)
        return 1
    try:
        db = StateDB(settings.output.state_db)
    except Exception as exc:
        log.error("初始化本地状态库失败: %s", exc)
        return 1

    # 候选：已登录 / 未使用，且 token 或已绑定窗口
    candidates: List[AccountRow] = []
    for row in store.rows:
        if row.status == STATUS_RESTRICTED:
            continue
        if row.status not in ("", STATUS_LOGINED, STATUS_UNUSED):
            continue
        if not row.token and not store.value_of(row, COL_PROFILE_ID):
            continue
        if db.warmup_count_today(_account_key(row)) >= max(1, cfg.daily_per_account):
            continue
        candidates.append(row)

    if not candidates:
        log.info("没有需要养号的账号（可能今天都已跑过），退出")
        return 0

    random.shuffle(candidates)
    if cfg.accounts_per_run > 0:
        candidates = candidates[: cfg.accounts_per_run]
    log.info("本次计划养号: %d 个账号", len(candidates))

    try:
        bm = BrowserManager(settings.ixbrowser)
        profiles = bm.list_profiles()
    except Exception as exc:
        log.error("连接 ixBrowser 失败: %s", exc)
        return 1

    keyword = settings.ixbrowser.profile_name_filter
    if keyword:
        profiles = [p for p in profiles if keyword in p["name"]]
    start = max(1, settings.ixbrowser.profile_start)
    end = settings.ixbrowser.profile_end
    if end >= start:
        profiles = profiles[start - 1 : end]
    else:
        profiles = profiles[start - 1 :]
    if not profiles:
        log.error("没有可用窗口")
        return 1

    id_to_name = {p["id"]: p["name"] for p in profiles}
    used: set = set()
    # 预留：被账号绑定的窗口只留给它自己
    reserved: Dict[str, str] = {}
    for candidate in candidates:
        bound = store.value_of(candidate, COL_PROFILE_ID)
        if bound and bound in id_to_name:
            reserved.setdefault(bound, _account_key(candidate))

    total = {"ok": 0, "skip": 0, "actions": 0, "likes": 0, "retweets": 0, "follows": 0}
    started = time.time()

    for index, row in enumerate(candidates):
        if stop_event is not None and stop_event.is_set():
            log.warning("收到停止信号，保存后退出 ...")
            break

        profile = _pick_profile(row, store, id_to_name, used, profiles, reserved)
        if profile is None:
            if store.value_of(row, COL_PROFILE_ID):
                log.warning(
                    "账号 %s 绑定的窗口不可用或已被占用，本次跳过", _account_key(row)
                )
                total["skip"] += 1
                continue
            log.warning("没有空闲窗口了，剩余账号留到下次")
            break
        used.add(profile["id"])

        log.info("=" * 60)
        log.info(
            "养号 %d/%d: %s -> 窗口 %s（%s）",
            index + 1,
            len(candidates),
            _account_key(row),
            profile["name"] or profile["id"],
            profile["id"],
        )
        stats = _process_account(bm, profile, row, settings, store, db, stop_event)
        if stats is None:
            total["skip"] += 1
        else:
            total["ok"] += 1
            total["actions"] += stats.actions
            total["likes"] += stats.likes
            total["retweets"] += stats.retweets
            total["follows"] += stats.follows

        if index < len(candidates) - 1 and (
            stop_event is None or not stop_event.is_set()
        ):
            rest = random.uniform(
                min(cfg.rest_min, cfg.rest_max), max(cfg.rest_min, cfg.rest_max)
            )
            log.info("(随机化) 休息 %d 秒后处理下一个账号 ...", int(rest))
            if not humanize.sleep_interruptible(rest, stop_event):
                break

    store.save()
    elapsed = int(time.time() - started)
    today = db.warmup_summary_today()
    log.info("=" * 60)
    log.info(
        "养号结束: 成功 %d / 跳过 %d，点赞 %d / 转推 %d / 关注 %d，用时 %ds",
        total["ok"],
        total["skip"],
        total["likes"],
        total["retweets"],
        total["follows"],
        elapsed,
    )
    log.info(
        "今日累计: 账号 %d 个 / 动作 %d 次 / 点赞 %d / 转推 %d / 关注 %d",
        today.get("accounts", 0),
        today.get("actions", 0),
        today.get("likes", 0),
        today.get("retweets", 0),
        today.get("follows", 0),
    )
    return 0

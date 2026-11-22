"""Twitter 批量登录 + MetaMask 钱包初始化（CLI 入口）。

用法:
    python main.py                          # 使用同目录 config.yaml
    python main.py --config config.yaml --limit 2
    python main.py --skip-wallet            # 只登录 Twitter
    python main.py --excel accounts.xlsx    # 临时指定 token 表

GUI 入口:  python gui.py  （Windows 上双击 gui.py 即可）

脚本必须运行在安装了 ixBrowser 客户端的 Windows 机器上。
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
import threading
import time
from pathlib import Path
from queue import Queue
from typing import Dict, List, Optional

from modules.browser import BrowserManager, IxBrowserError
from modules.config import Settings, load_settings
from modules.excel_store import (
    COL_FINAL,
    COL_PASSWORD_LOGIN,
    COL_PROFILE_ID,
    COL_PROFILE_NAME,
    COL_STATUS,
    COL_TOKEN_LOGIN,
    COL_USERNAME,
    PASSWORD_FAILED,
    PASSWORD_UNKNOWN,
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_LOGINED,
    STATUS_RESTRICTED,
    TOKEN_FAILED,
    TOKEN_OK,
    TOKEN_UNKNOWN,
    AccountRow,
    ExcelStore,
)
from modules.state_db import StateDB
from modules import tw_login, twofa, wallet

log = logging.getLogger("x-batch")


# ----------------------------------------------------------------- 日志系统
class QueueLogHandler(logging.Handler):
    """把 log 记录推到一个 Queue，GUI 线程从 Queue 读出来显示。"""

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
) -> None:
    log_dir = Path(settings.output.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"run_{time.strftime('%Y%m%d_%H%M%S')}.log"

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


# ------------------------------------------------------------------ 辅助
def mask(token: str) -> str:
    return f"***{token[-4:]}" if len(token) > 4 else "***"


def human_delay(settings: Settings, stop_event: Optional[threading.Event] = None) -> None:
    low = min(settings.twitter.delay_min, settings.twitter.delay_max)
    high = max(settings.twitter.delay_min, settings.twitter.delay_max)
    target = random.uniform(low, high)
    if stop_event is None:
        time.sleep(target)
        return
    # 支持中途停止
    end = time.time() + target
    while time.time() < end and not stop_event.is_set():
        time.sleep(0.2)


def handle_wallet(
    browser,
    context,
    settings: Settings,
    store: ExcelStore,
    row: AccountRow,
    label: str,
) -> None:
    if not settings.wallet.enabled or settings.wallet.mode == "skip":
        return
    log.info("  开始处理钱包 ...")
    result = wallet.ensure_wallet(
        browser, context, settings.wallet, settings.output.screenshot_dir, label
    )

    if result.status == "ok":
        store.update(
            row,
            钱包地址=result.address,
            助记词=result.mnemonic,
            钱包密码=result.password,
            备注="",
        )
        log.info("  钱包完成: %s", result.address)
    elif result.status == "manual":
        store.update(
            row,
            钱包地址=result.address,
            助记词=result.mnemonic,
            备注=f"钱包需人工处理: {result.note}",
        )
        log.warning("  钱包转人工: %s", result.note)
    elif result.status == "exists":
        store.update(row, 备注="窗口内已存在钱包，未覆盖")
        log.warning("  窗口内已存在钱包，跳过")
    elif result.status == "skip":
        return
    else:
        store.update(row, 备注=f"钱包失败: {result.note}")
        log.error("  钱包失败: %s", result.note)
    store.save()


# ---------------------------------------------------------------- 单窗口流程
def _sleep(seconds: float, stop_event: Optional[threading.Event]) -> bool:
    """可中断的等待，返回 False 表示期间收到了停止信号。"""
    end = time.time() + seconds
    while time.time() < end:
        if stop_event is not None and stop_event.is_set():
            return False
        time.sleep(0.2)
    return True


def _token_text(result) -> str:
    if result is None:
        return TOKEN_UNKNOWN
    return {
        tw_login.OK: TOKEN_OK,
        tw_login.INVALID: TOKEN_FAILED,
        tw_login.RESTRICTED: "受限",
        tw_login.ERROR: "异常",
    }.get(result.status, "异常")


def _password_text(result) -> str:
    if result is None:
        return PASSWORD_UNKNOWN
    return {
        tw_login.OK: "成功",
        tw_login.INVALID: PASSWORD_FAILED,
        tw_login.RESTRICTED: "受限",
        tw_login.ERROR: "异常",
    }.get(result.status, "异常")


def process_profile(
    bm: BrowserManager,
    profile: Dict[str, str],
    pending: List[AccountRow],
    settings: Settings,
    store: ExcelStore,
    db: StateDB,
    stats: Dict[str, int],
    stop_event: Optional[threading.Event] = None,
) -> None:
    profile_id = profile["id"]
    profile_name = profile["name"]
    label = profile_name or profile_id

    log.info("=" * 60)
    log.info("打开窗口: %s (%s)", label, profile_id)
    try:
        debugging = bm.open(profile_id)
        time.sleep(1.0)
        browser, context = bm.connect(debugging)
    except Exception as exc:
        stats["window_error"] += 1
        log.error("  打开/连接窗口失败: %s", exc)
        bm.stop_playwright()
        return

    tried = 0
    try:
        while pending and tried < settings.twitter.max_tokens_per_profile:
            if stop_event is not None and stop_event.is_set():
                log.warning("  收到停止信号，结束当前窗口")
                return
            row = pending[0]
            tried += 1
            record_label = row.idx or str(row.sheet_row)
            log.info(
                "  处理记录 %s（Excel 行 %d，账号 %s）",
                record_label,
                row.sheet_row,
                row.account or "(无)",
            )

            # ============ 第一步：token 登录 ============
            token_result = None
            if row.token:
                log.info("    尝试 token %s", mask(row.token))
                attempt = 0
                while True:
                    try:
                        token_result = tw_login.login_with_token(
                            context,
                            row.token,
                            home_url=settings.twitter.home_url,
                            timeout=settings.twitter.login_timeout,
                            ct0=store.ct0_of(row),
                        )
                    except Exception as exc:
                        token_result = tw_login.LoginResult(tw_login.ERROR, detail=str(exc))

                    if token_result.status != tw_login.ERROR:
                        break
                    if attempt >= settings.twitter.retry_per_token:
                        break
                    attempt += 1
                    log.warning("    token 加载异常，重试中: %s", token_result.detail)
                    if not _sleep(3.0, stop_event):
                        return
            token_status = _token_text(token_result)

            store.update(
                row,
                **{
                    COL_TOKEN_LOGIN: token_status,
                    COL_PROFILE_ID: profile_id,
                    COL_PROFILE_NAME: profile_name,
                },
            )
            db.upsert(
                row.sheet_row,
                idx=row.idx,
                account=row.account,
                token_status=token_status,
                profile=label,
                note=token_result.detail if token_result else "",
            )

            if token_result is not None and token_result.status == tw_login.OK:
                log.info("    token 登录成功: %s", token_result.username or "(未取到用户名)")
                _finish_success(store, db, row, profile_id, profile_name, token_status,
                                PASSWORD_UNKNOWN, token_result.username, "token")
                pending.pop(0)
                store.save()
                stats["ok"] += 1
                stats["token_ok"] += 1
                _after_login(browser, context, settings, store, row, label)
                return

            if token_status != TOKEN_UNKNOWN:
                log.warning("    token 登录失败: %s", token_result.detail)

            # ============ 第二步：账号密码兜底 ============
            password_result = None
            can_password = (
                settings.twitter.password_login
                and bool(row.account)
                and bool(row.password)
                and token_status != "受限"
            )
            if can_password:
                def _code_getter() -> str:
                    code, source = twofa.code_from_link(row.twofa)
                    log.info("    2FA 验证码来源: %s", source)
                    return code

                log.info("    改用账号密码登录（账号 %s）...", row.account)
                try:
                    password_result = tw_login.login_with_password(
                        context,
                        row.account,
                        row.password,
                        _code_getter,
                        login_url=settings.twitter.login_url,
                        timeout=settings.twitter.password_timeout,
                    )
                except Exception as exc:
                    password_result = tw_login.LoginResult(tw_login.ERROR, detail=str(exc))

                password_status = _password_text(password_result)
                store.update(row, **{COL_PASSWORD_LOGIN: password_status})
                db.upsert(row.sheet_row, password_status=password_status,
                          note=password_result.detail)

                if password_result.status == tw_login.OK:
                    log.info("    账号密码登录成功: %s", password_result.username or row.account)
                    _finish_success(store, db, row, profile_id, profile_name, token_status,
                                    password_status, password_result.username, "password")
                    pending.pop(0)
                    store.save()
                    stats["ok"] += 1
                    stats["pwd_ok"] += 1
                    _after_login(browser, context, settings, store, row, label)
                    return

                log.warning("    账号密码登录失败: %s", password_result.detail)
            else:
                password_status = PASSWORD_UNKNOWN
                log.info("    跳过账号密码登录（未启用或缺少账号/密码）")
                store.update(row, **{COL_PASSWORD_LOGIN: PASSWORD_UNKNOWN})

            # ============ 第三步：两种方式都失败 ============
            status = _final_status(token_result, password_result)
            token_detail = token_result.detail if token_result else ""
            pwd_detail = password_result.detail if password_result else ""
            note = " | ".join(x for x in (token_detail, pwd_detail) if x)
            store.update(
                row,
                **{
                    COL_STATUS: status,
                    COL_FINAL: status,
                    COL_PROFILE_ID: profile_id,
                    COL_PROFILE_NAME: profile_name,
                    "备注": note,
                },
            )
            db.upsert(row.sheet_row, final_status=status, note=note)
            pending.pop(0)
            store.save()

            if status == STATUS_RESTRICTED:
                stats["restricted"] += 1
                log.warning("  token / 密码均不可用（受限），换下一条: %s", note)
            elif status == STATUS_ERROR:
                stats["error"] += 1
                log.error("  网络/页面异常，放弃当前窗口: %s", note)
                return
            else:
                stats["fail"] += 1
                log.warning("  全部登录方式失败，换下一条: %s", note)
            human_delay(settings, stop_event)
    finally:
        bm.stop_playwright()
        if not settings.ixbrowser.keep_window_open:
            try:
                bm.close(profile_id)
                log.info("  窗口已关闭")
            except Exception:
                pass


def _final_status(token_result, password_result) -> str:
    statuses = [r.status for r in (token_result, password_result) if r is not None]
    if tw_login.RESTRICTED in statuses:
        return STATUS_RESTRICTED
    if tw_login.ERROR in statuses:
        return STATUS_ERROR
    return STATUS_FAILED


def _finish_success(
    store: ExcelStore,
    db: StateDB,
    row: AccountRow,
    profile_id: str,
    profile_name: str,
    token_status: str,
    password_status: str,
    username: str,
    method: str,
) -> None:
    store.update(
        row,
        **{
            COL_STATUS: STATUS_LOGINED,
            COL_FINAL: STATUS_LOGINED,
            COL_TOKEN_LOGIN: token_status,
            COL_PASSWORD_LOGIN: password_status,
            COL_PROFILE_ID: profile_id,
            COL_PROFILE_NAME: profile_name,
            COL_USERNAME: username,
            "备注": f"登录方式: {method}",
        },
    )
    db.upsert(
        row.sheet_row,
        token_status=token_status,
        password_status=password_status,
        final_status=STATUS_LOGINED,
        username=username,
        profile=profile_name or profile_id,
        note=f"登录方式: {method}",
    )


def _after_login(browser, context, settings: Settings, store: ExcelStore,
                 row: AccountRow, label: str) -> None:
    try:
        handle_wallet(browser, context, settings, store, row, label)
    except Exception as exc:
        store.update(row, 备注=f"钱包处理异常: {exc}")
        store.save()
        log.exception("  钱包处理异常")


# ---------------------------------------------------------------- 总流程
def run(
    settings: Settings,
    gui_queue: Optional[Queue] = None,
    stop_event: Optional[threading.Event] = None,
) -> int:
    """核心流程：CLI 和 GUI 都调用这个函数。返回 0 成功 / 非 0 失败。"""
    setup_logging(settings, gui_queue)

    try:
        store = ExcelStore(settings.excel)
    except Exception as exc:
        log.error("读取 Excel 失败: %s", exc)
        return 1

    if store.is_write_locked():
        log.warning(
            "原表正在被 Excel/WPS 打开，状态无法写回原表（会自动另存为 _results.xlsx）。"
            "建议先关闭表格再运行。"
        )

    try:
        db = StateDB(settings.output.state_db)
    except Exception as exc:
        log.error("初始化本地状态库失败: %s", exc)
        return 1

    pending = store.pending_rows()
    log.info("待处理记录: %d 个（总行数 %d）", len(pending), len(store.rows))
    if not pending:
        log.info("没有待处理的记录，退出")
        return 0

    try:
        bm = BrowserManager(settings.ixbrowser)
        profiles = bm.list_profiles()
    except Exception as exc:
        log.error("连接 ixBrowser 失败: %s", exc)
        return 1

    keyword = settings.ixbrowser.profile_name_filter
    if keyword:
        profiles = [p for p in profiles if keyword in p["name"]]
    if settings.ixbrowser.profile_limit > 0:
        profiles = profiles[: settings.ixbrowser.profile_limit]

    log.info("可用窗口: %d 个", len(profiles))
    if not profiles:
        log.error("没有可用窗口，请检查 ixBrowser 窗口列表或过滤条件")
        return 1

    stats = {
        "ok": 0,
        "token_ok": 0,
        "pwd_ok": 0,
        "fail": 0,
        "restricted": 0,
        "error": 0,
        "window_error": 0,
    }
    started = time.time()
    try:
        for profile in profiles:
            if stop_event is not None and stop_event.is_set():
                log.warning("用户请求停止，保存后退出 ...")
                break
            if not pending:
                break
            process_profile(bm, profile, pending, settings, store, db, stats, stop_event)
    except KeyboardInterrupt:
        log.warning("收到中断，正在保存 ...")
    finally:
        store.save()

    elapsed = int(time.time() - started)
    log.info("=" * 60)
    log.info(
        "完成: 成功 %d（token %d / 密码 %d）/ 全部失败 %d / 受限 %d / 异常 %d / 窗口失败 %d，用时 %ds",
        stats["ok"],
        stats["token_ok"],
        stats["pwd_ok"],
        stats["fail"],
        stats["restricted"],
        stats["error"],
        stats["window_error"],
        elapsed,
    )
    log.info("表格统计: %s", store.summary())
    log.info("本地记录库: %s", db.path.resolve())
    log.info("结果文件: %s", store.path.resolve())
    log.info("剩余未处理 token: %d 个", len(pending))
    return 0 if stats["ok"] + stats["fail"] + stats["restricted"] > 0 else 1


# ------------------------------------------------------------------ CLI
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ixBrowser 推特批量登录 + MetaMask 初始化")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--excel", default="", help="临时覆盖 token 表路径")
    parser.add_argument("--limit", type=int, default=-1, help="本次最多处理多少个窗口")
    parser.add_argument("--skip-wallet", action="store_true", help="本次不处理钱包")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_settings(args.config)
    if args.excel:
        settings.excel.path = args.excel
    if args.limit >= 0:
        settings.ixbrowser.profile_limit = args.limit
    if args.skip_wallet:
        settings.wallet.enabled = False
    return run(settings)


if __name__ == "__main__":
    raise SystemExit(main())

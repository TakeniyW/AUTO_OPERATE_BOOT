"""Twitter 批量登录 + MetaMask 钱包初始化。

用法:
    python main.py                          # 使用同目录 config.yaml
    python main.py --config config.yaml --limit 2
    python main.py --skip-wallet            # 只登录 Twitter
    python main.py --excel accounts.xlsx    # 临时指定 token 表

脚本必须运行在安装了 ixBrowser 客户端的 Windows 机器上。
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from pathlib import Path
from typing import Dict, List

from modules.browser import BrowserManager, IxBrowserError
from modules.config import Settings, load_settings
from modules.excel_store import (
    COL_PROFILE_ID,
    COL_PROFILE_NAME,
    COL_STATUS,
    COL_USERNAME,
    STATUS_ERROR,
    STATUS_INVALID,
    STATUS_LOGINED,
    STATUS_RESTRICTED,
    AccountRow,
    ExcelStore,
)
from modules import tw_login, wallet

log = logging.getLogger("x-batch")


def setup_logging(settings: Settings) -> None:
    log_dir = Path(settings.output.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"run_{time.strftime('%Y%m%d_%H%M%S')}.log"

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(file_handler)


def mask(token: str) -> str:
    return f"***{token[-4:]}" if len(token) > 4 else "***"


def human_delay(settings: Settings) -> None:
    low = min(settings.twitter.delay_min, settings.twitter.delay_max)
    high = max(settings.twitter.delay_min, settings.twitter.delay_max)
    time.sleep(random.uniform(low, high))


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


def process_profile(
    bm: BrowserManager,
    profile: Dict[str, str],
    pending: List[AccountRow],
    settings: Settings,
    store: ExcelStore,
    stats: Dict[str, int],
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
    error_tries = 0
    try:
        while pending and tried < settings.twitter.max_tokens_per_profile:
            row = pending[0]
            tried += 1
            log.info("  尝试 token %s（第 %d 个，Excel 行 %d）", mask(row.token), tried, row.sheet_row)

            try:
                result = tw_login.login_with_token(
                    context,
                    row.token,
                    home_url=settings.twitter.home_url,
                    timeout=settings.twitter.login_timeout,
                    ct0=store.ct0_of(row),
                )
            except Exception as exc:
                result = tw_login.LoginResult(tw_login.ERROR, detail=str(exc))

            if result.status == tw_login.OK:
                store.update(
                    row,
                    **{
                        COL_STATUS: STATUS_LOGINED,
                        COL_PROFILE_ID: profile_id,
                        COL_PROFILE_NAME: profile_name,
                        COL_USERNAME: result.username,
                        "备注": "",
                    },
                )
                pending.pop(0)
                store.save()
                stats["ok"] += 1
                log.info("  登录成功: %s", result.username or "(未取到用户名)")
                try:
                    handle_wallet(browser, context, settings, store, row, label)
                except Exception as exc:
                    store.update(row, 备注=f"钱包处理异常: {exc}")
                    store.save()
                    log.exception("  钱包处理异常")
                return

            if result.status == tw_login.INVALID:
                store.update(
                    row,
                    **{
                        COL_STATUS: STATUS_INVALID,
                        COL_PROFILE_ID: profile_id,
                        COL_PROFILE_NAME: profile_name,
                        "备注": result.detail,
                    },
                )
                pending.pop(0)
                store.save()
                stats["invalid"] += 1
                error_tries = 0
                log.warning("  token 失效: %s", result.detail)
                human_delay(settings)
                continue

            if result.status == tw_login.RESTRICTED:
                store.update(
                    row,
                    **{
                        COL_STATUS: STATUS_RESTRICTED,
                        COL_PROFILE_ID: profile_id,
                        COL_PROFILE_NAME: profile_name,
                        "备注": result.detail,
                    },
                )
                pending.pop(0)
                store.save()
                stats["restricted"] += 1
                error_tries = 0
                log.warning("  账号受限，换下一个 token: %s", result.detail)
                human_delay(settings)
                continue

            # 网络/加载异常：同一 token 先重试，仍失败则放弃该窗口
            error_tries += 1
            if error_tries <= settings.twitter.retry_per_token:
                log.warning("  加载异常，%d 秒后重试: %s", 5, result.detail)
                time.sleep(5)
                continue

            store.update(
                row,
                **{
                    COL_STATUS: STATUS_ERROR,
                    COL_PROFILE_ID: profile_id,
                    COL_PROFILE_NAME: profile_name,
                    "备注": result.detail,
                },
            )
            pending.pop(0)
            store.save()
            stats["error"] += 1
            log.error("  放弃当前窗口: %s", result.detail)
            return
    finally:
        bm.stop_playwright()
        if not settings.ixbrowser.keep_window_open:
            bm.close(profile_id)
            log.info("  窗口已关闭")


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

    setup_logging(settings)
    log.info("配置: %s", Path(args.config).resolve())

    try:
        store = ExcelStore(settings.excel)
    except Exception as exc:
        log.error("读取 Excel 失败: %s", exc)
        return 1

    pending = store.pending_rows()
    log.info("待处理 token: %d 个（总行数 %d）", len(pending), len(store.rows))
    if not pending:
        log.info("没有待处理的 token，退出")
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

    stats = {"ok": 0, "invalid": 0, "restricted": 0, "error": 0, "window_error": 0}
    started = time.time()
    try:
        for profile in profiles:
            if not pending:
                break
            process_profile(bm, profile, pending, settings, store, stats)
    except KeyboardInterrupt:
        log.warning("收到中断，正在保存 ...")
    finally:
        store.save()

    elapsed = int(time.time() - started)
    log.info("=" * 60)
    log.info(
        "完成: 成功 %d / 失效 %d / 受限 %d / 异常 %d / 窗口失败 %d，用时 %ds",
        stats["ok"],
        stats["invalid"],
        stats["restricted"],
        stats["error"],
        stats["window_error"],
        elapsed,
    )
    log.info("表格统计: %s", store.summary())
    log.info("结果文件: %s", store.path.resolve())
    log.info("剩余未处理 token: %d 个", len(pending))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

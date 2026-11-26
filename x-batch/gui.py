"""桌面 GUI：ixBrowser 批量推特登录 + MetaMask 钱包初始化。

启动:   python gui.py
打包exe: pip install pyinstaller && pyinstaller -F -w --name x-batch gui.py
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path
from queue import Empty
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "x-batch v1.4.3 · ixBrowser 批量推特登录 + 钱包初始化 + 养号"

_WORKDIR = Path(__file__).parent.resolve()
_STARTUP_LOG = _WORKDIR / "startup_error.log"

# 与 modules/applog.STATS_PREFIX 保持一致：GUI 拦截该前缀的结构化统计消息
STATS_PREFIX = "@@STATS "

# 真正的对象在 main() 里通过 _load_core() 注入：
# 这样缺依赖时能弹窗说明原因，而不是双击后窗口一闪而过。
run = None
Settings = None
load_settings = None


def _report_startup_error(title: str, detail: str) -> None:
    """双击启动时没有控制台，把 traceback 写入文件并弹窗，避免闪退后无从排查。"""
    try:
        with _STARTUP_LOG.open("a", encoding="utf-8") as fp:
            fp.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n{detail}\n")
    except Exception:
        pass
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            title,
            f"{detail[-1500:]}\n\n详细信息已写入:\n{_STARTUP_LOG}",
        )
        root.destroy()
    except Exception:
        print(detail, file=sys.stderr)


def _check_dependencies() -> None:
    """只检查启动必需的硬依赖；playwright 等在实际运行时按需报错。"""
    import importlib.util

    missing = [
        pkg
        for mod, pkg in (("yaml", "PyYAML"), ("openpyxl", "openpyxl"))
        if importlib.util.find_spec(mod) is None
    ]
    if missing:
        raise RuntimeError(
            "缺少运行依赖: " + ", ".join(missing) + "\n\n"
            "请双击本目录的 install_deps.bat（安装依赖），或在命令行执行:\n"
            "  pip install -r requirements.txt\n\n"
            "国内网络慢可加镜像:\n"
            "  pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple"
        )


def _load_core():
    _check_dependencies()
    from main import run as _run
    from modules.config import Settings as _Settings, load_settings as _load

    return _run, _Settings, _load

# 颜色
COLOR_RUNNING = "#c0392b"
COLOR_IDLE = "#27ae60"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1020x760")
        self.minsize(900, 680)

        self._workdir = Path(__file__).parent.resolve()
        self._config_path = self._workdir / "config.yaml"

        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._log_queue: queue.Queue = queue.Queue()

        self._settings = load_settings(str(self._config_path))
        self._is_running = False
        self._rec_rows: list = []
        self._db = None
        self._db_path = ""

        self._build_widgets()
        self._load_settings_to_ui()
        self._refresh_records()
        self.after(200, self._drain_log_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----------------------------------------------------------- UI 构建
    def _build_widgets(self) -> None:
        # 顶部：状态栏 + 开始/停止
        top = ttk.Frame(self, padding=(10, 10, 10, 0))
        top.pack(fill=tk.X)

        self._status_dot = tk.Label(top, text="●", fg=COLOR_IDLE, font=("Consolas", 14))
        self._status_dot.pack(side=tk.LEFT)
        self._status_label = ttk.Label(top, text="空闲", font=("", 12))
        self._status_label.pack(side=tk.LEFT, padx=(6, 0))

        self._start_btn = ttk.Button(top, text="▶ 开始", width=10, command=self._on_start)
        self._start_btn.pack(side=tk.RIGHT)
        self._stop_btn = ttk.Button(top, text="■ 停止", width=10, command=self._on_stop, state=tk.DISABLED)
        self._stop_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self._save_cfg_btn = ttk.Button(top, text="保存配置", command=self._on_save_cfg)
        self._save_cfg_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self._load_cfg_btn = ttk.Button(top, text="加载配置", command=self._on_load_cfg)
        self._load_cfg_btn.pack(side=tk.RIGHT, padx=(0, 8))

        # 主体：参数 Notebook (左 50%) + 日志/统计 (右 50%)
        body = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # ---- 左：参数
        left = ttk.Frame(body, padding=6)
        body.add(left, weight=1)
        nb = ttk.Notebook(left)
        nb.pack(fill=tk.BOTH, expand=True)

        self._tab_excel = self._build_excel_tab(nb)
        self._tab_ix = self._build_ix_tab(nb)
        self._tab_tw = self._build_tw_tab(nb)
        self._tab_wallet = self._build_wallet_tab(nb)
        self._tab_random = self._build_random_tab(nb)
        self._tab_warmup = self._build_warmup_tab(nb)
        self._tab_records = self._build_records_tab(nb)

        nb.add(self._tab_excel, text="① Excel")
        nb.add(self._tab_ix, text="② ixBrowser")
        nb.add(self._tab_tw, text="③ Twitter")
        nb.add(self._tab_wallet, text="④ 钱包")
        nb.add(self._tab_random, text="⑤ 随机化")
        nb.add(self._tab_warmup, text="⑥ 养号")
        nb.add(self._tab_records, text="⑦ 记录查询")

        # ---- 右：统计 + 日志
        right = ttk.Frame(body, padding=(6, 6, 0, 6))
        body.add(right, weight=1)

        stats_frame = ttk.LabelFrame(right, text="实时统计", padding=8)
        stats_frame.pack(fill=tk.X)

        self._stat_vars = {}
        for key, label in [
            ("ok", "✅ 登录成功(总)"),
            ("token_ok", "token 登录成功"),
            ("pwd_ok", "密码登录成功"),
            ("fail", "❌ 全部失败"),
            ("restricted", "⚠️ 受限"),
            ("error", "❗ 异常"),
            ("window_error", "窗口失败"),
            ("warm_ok", "🌱 养号账号"),
            ("warm_likes", "👍 点赞"),
            ("warm_retweets", "🔁 转推"),
            ("warm_follows", "👥 关注"),
        ]:
            row = ttk.Frame(stats_frame)
            row.pack(fill=tk.X, pady=1)
            ttk.Label(row, text=label, width=14).pack(side=tk.LEFT)
            var = tk.StringVar(value="0")
            ttk.Label(row, textvariable=var, foreground="#2980b9", font=("", 12, "bold")).pack(side=tk.LEFT)
            self._stat_vars[key] = var

        prog_row = ttk.Frame(stats_frame)
        prog_row.pack(fill=tk.X, pady=(6, 0))
        self._progress_var = tk.StringVar(value="进度 0/0")
        ttk.Label(prog_row, textvariable=self._progress_var, width=14).pack(side=tk.LEFT)
        self._progress_bar = ttk.Progressbar(prog_row, mode="determinate", maximum=1, value=0)
        self._progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        action_row = ttk.Frame(right)
        action_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(action_row, text="打开结果表", command=self._open_result_table).pack(side=tk.LEFT)
        ttk.Button(action_row, text="日志目录", command=self._open_log_dir).pack(side=tk.LEFT, padx=6)
        ttk.Button(action_row, text="截图目录", command=self._open_screenshot_dir).pack(side=tk.LEFT)

        log_frame = ttk.LabelFrame(right, text="运行日志", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self._log_text = tk.Text(log_frame, wrap="word", height=18, state=tk.DISABLED,
                                 font=("Consolas", 10), background="#fafafa")
        self._log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(log_frame, command=self._log_text.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._log_text.config(yscrollcommand=sb.set)
        self._log_text.tag_configure("INFO", foreground="#2c3e50")
        self._log_text.tag_configure("WARNING", foreground="#d35400")
        self._log_text.tag_configure("ERROR", foreground="#c0392b")

    # -------------------------------------------------------- 各 Tab 构建
    def _build_excel_tab(self, parent: ttk.Widget) -> ttk.Frame:
        f = ttk.Frame(parent, padding=10)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="token 表路径").grid(row=0, column=0, sticky="w", pady=4)
        self._excel_var = tk.StringVar()
        ttk.Entry(f, textvariable=self._excel_var).grid(row=0, column=1, sticky="ew", pady=4, padx=4)
        ttk.Button(f, text="浏览…", command=self._pick_excel).grid(row=0, column=2, pady=4)

        ttk.Label(f, text="token 列名").grid(row=1, column=0, sticky="w", pady=4)
        self._token_col_var = tk.StringVar(value="auth_token")
        ttk.Entry(f, textvariable=self._token_col_var).grid(row=1, column=1, sticky="ew", pady=4, padx=4)

        ttk.Label(f, text="ct0 列名(可空)").grid(row=2, column=0, sticky="w", pady=4)
        self._ct0_col_var = tk.StringVar()
        ttk.Entry(f, textvariable=self._ct0_col_var).grid(row=2, column=1, sticky="ew", pady=4, padx=4)

        ttk.Label(f, text="账号列名").grid(row=3, column=0, sticky="w", pady=4)
        self._user_col_var = tk.StringVar(value="账号")
        self._user_entry = ttk.Entry(f, textvariable=self._user_col_var)
        self._user_entry.grid(row=3, column=1, sticky="ew", pady=4, padx=4)

        ttk.Label(f, text="密码列名").grid(row=4, column=0, sticky="w", pady=4)
        self._pwd_col_var = tk.StringVar(value="密码")
        self._pwd_entry = ttk.Entry(f, textvariable=self._pwd_col_var)
        self._pwd_entry.grid(row=4, column=1, sticky="ew", pady=4, padx=4)

        ttk.Label(f, text="2FA 链接列名").grid(row=5, column=0, sticky="w", pady=4)
        self._twofa_col_var = tk.StringVar(value="2FA")
        self._twofa_entry = ttk.Entry(f, textvariable=self._twofa_col_var)
        self._twofa_entry.grid(row=5, column=1, sticky="ew", pady=4, padx=4)

        self._pwd_hint_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=self._pwd_hint_var, foreground="#7f8c8d").grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(2, 0)
        )

        ttk.Label(f, text="工作表名(可空)").grid(row=7, column=0, sticky="w", pady=4)
        self._sheet_var = tk.StringVar()
        ttk.Entry(f, textvariable=self._sheet_var).grid(row=7, column=1, sticky="ew", pady=4, padx=4)
        ttk.Label(f, text="(留空=第一个)", foreground="#7f8c8d").grid(row=7, column=2, sticky="w", pady=4)

        ttk.Label(f, text="行号范围").grid(row=8, column=0, sticky="w", pady=4)
        row_range = ttk.Frame(f)
        row_range.grid(row=8, column=1, sticky="w", pady=4, padx=4)
        self._excel_row_start_var = tk.StringVar(value="0")
        self._excel_row_end_var = tk.StringVar(value="0")
        ttk.Entry(row_range, textvariable=self._excel_row_start_var, width=8).pack(side=tk.LEFT)
        ttk.Label(row_range, text="~").pack(side=tk.LEFT, padx=4)
        ttk.Entry(row_range, textvariable=self._excel_row_end_var, width=8).pack(side=tk.LEFT)
        ttk.Label(f, text="(Excel 行号；0=不限)", foreground="#7f8c8d").grid(
            row=8, column=2, sticky="w", pady=4
        )

        opts_row = ttk.Frame(f)
        opts_row.grid(row=9, column=0, columnspan=3, sticky="w", pady=4)
        self._backup_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opts_row, text="首次运行前备份原表 (.bak)", variable=self._backup_var
        ).pack(side=tk.LEFT)
        self._excel_retry_failed_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opts_row, text="仅重跑失败/受限/异常行", variable=self._excel_retry_failed_var
        ).pack(side=tk.LEFT, padx=(12, 0))

        dl_row = ttk.Frame(f)
        dl_row.grid(row=10, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Button(
            dl_row, text="⬇ 下载登录模版", command=self._on_download_template
        ).pack(side=tk.LEFT)
        self._download_result_btn = ttk.Button(
            dl_row, text="⬇ 下载执行结果 Excel", command=self._on_download_result
        )
        self._download_result_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._reset_failed_btn = ttk.Button(
            dl_row, text="↺ 重置失败行", command=self._on_reset_failed
        )
        self._reset_failed_btn.pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(
            f,
            text="提示: 只跑 token 时表里只需 token 一列；启用账号密码登录后还需 账号 / 密码 / 2FA。\n"
                 "「下载登录模版」生成标准表头；「下载执行结果 Excel」会另存副本，不改动原表。\n"
                 "「重置失败行」会清空失败/受限/异常行的状态列，便于下次重跑；原始 token 不受影响。",
            foreground="#7f8c8d",
            wraplength=440,
            justify="left",
        ).grid(row=11, column=0, columnspan=3, sticky="w", pady=(10, 0))
        return f

    def _build_ix_tab(self, parent: ttk.Widget) -> ttk.Frame:
        f = ttk.Frame(parent, padding=10)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="ixBrowser API 地址").grid(row=0, column=0, sticky="w", pady=4)
        api_row = ttk.Frame(f); api_row.grid(row=0, column=1, sticky="ew", pady=4, padx=4)
        self._ix_host_var = tk.StringVar(value="127.0.0.1")
        self._ix_port_var = tk.StringVar(value="53200")
        ttk.Entry(api_row, textvariable=self._ix_host_var, width=14).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(api_row, text=":").pack(side=tk.LEFT)
        ttk.Entry(api_row, textvariable=self._ix_port_var, width=7).pack(side=tk.LEFT)

        ttk.Button(f, text="测试连接", command=self._test_ix).grid(row=0, column=2, pady=4)

        ttk.Label(f, text="窗口名称过滤").grid(row=1, column=0, sticky="w", pady=4)
        self._ix_filter_var = tk.StringVar()
        ttk.Entry(f, textvariable=self._ix_filter_var).grid(row=1, column=1, sticky="ew", pady=4, padx=4)

        ttk.Label(f, text="窗口序号范围").grid(row=2, column=0, sticky="w", pady=4)
        range_row = ttk.Frame(f)
        range_row.grid(row=2, column=1, sticky="w", pady=4, padx=4)
        self._ix_start_var = tk.StringVar(value="1")
        self._ix_end_var = tk.StringVar(value="0")
        ttk.Entry(range_row, textvariable=self._ix_start_var, width=6).pack(side=tk.LEFT)
        ttk.Label(range_row, text="~").pack(side=tk.LEFT, padx=4)
        ttk.Entry(range_row, textvariable=self._ix_end_var, width=6).pack(side=tk.LEFT)
        ttk.Label(f, text="(1 开始；结束 0 = 到最后一个)", foreground="#7f8c8d").grid(
            row=2, column=2, sticky="w", pady=4
        )

        ttk.Label(f, text="本次处理窗口数").grid(row=3, column=0, sticky="w", pady=4)
        self._ix_limit_var = tk.StringVar(value="1")
        ttk.Entry(f, textvariable=self._ix_limit_var, width=10).grid(row=3, column=1, sticky="w", pady=4, padx=4)
        ttk.Label(f, text="(0=全部)", foreground="#7f8c8d").grid(row=3, column=2, sticky="w", pady=4)

        self._keep_open_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="完成后保留窗口(调试用)", variable=self._keep_open_var).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=4
        )

        ttk.Label(
            f,
            text="序号按 ixBrowser 窗口列表顺序（一般是创建顺序），点“测试连接”可查看对应关系。",
            foreground="#7f8c8d",
            wraplength=380,
            justify="left",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))
        return f

    def _build_tw_tab(self, parent: ttk.Widget) -> ttk.Frame:
        f = ttk.Frame(parent, padding=10)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="登录超时 (秒)").grid(row=0, column=0, sticky="w", pady=4)
        self._tw_timeout_var = tk.StringVar(value="25")
        ttk.Entry(f, textvariable=self._tw_timeout_var, width=10).grid(row=0, column=1, sticky="w", pady=4, padx=4)

        ttk.Label(f, text="单窗口最多尝试 token 数").grid(row=1, column=0, sticky="w", pady=4)
        self._tw_max_var = tk.StringVar(value="20")
        ttk.Entry(f, textvariable=self._tw_max_var, width=10).grid(row=1, column=1, sticky="w", pady=4, padx=4)

        ttk.Label(f, text="同 token 网络异常重试次数").grid(row=2, column=0, sticky="w", pady=4)
        self._tw_retry_var = tk.StringVar(value="1")
        ttk.Entry(f, textvariable=self._tw_retry_var, width=10).grid(row=2, column=1, sticky="w", pady=4, padx=4)

        ttk.Label(f, text="动作间随机延迟").grid(row=3, column=0, sticky="w", pady=4)
        delay_row = ttk.Frame(f); delay_row.grid(row=3, column=1, sticky="w", pady=4, padx=4)
        self._tw_delay_min_var = tk.StringVar(value="3.0")
        self._tw_delay_max_var = tk.StringVar(value="6.0")
        ttk.Entry(delay_row, textvariable=self._tw_delay_min_var, width=6).pack(side=tk.LEFT)
        ttk.Label(delay_row, text="~").pack(side=tk.LEFT, padx=2)
        ttk.Entry(delay_row, textvariable=self._tw_delay_max_var, width=6).pack(side=tk.LEFT)
        ttk.Label(delay_row, text="秒", foreground="#7f8c8d").pack(side=tk.LEFT, padx=4)

        self._tw_pwd_login_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            f,
            text="token 登录失败时，改用账号密码登录（自动获取 2FA 验证码）",
            variable=self._tw_pwd_login_var,
            command=self._on_pwd_login_toggle,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 4))

        ttk.Label(f, text="账号密码登录超时 (秒)").grid(row=5, column=0, sticky="w", pady=4)
        self._tw_pwd_timeout_var = tk.StringVar(value="40")
        ttk.Entry(f, textvariable=self._tw_pwd_timeout_var, width=10).grid(
            row=5, column=1, sticky="w", pady=4, padx=4
        )

        ttk.Label(
            f,
            text="2FA 链接里的 key 会按标准 TOTP 在本地计算验证码；链接打不开也不影响使用。",
            foreground="#7f8c8d",
            wraplength=380,
            justify="left",
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 0))
        return f

    def _build_wallet_tab(self, parent: ttk.Widget) -> ttk.Frame:
        f = ttk.Frame(parent, padding=10)
        f.columnconfigure(1, weight=1)

        self._wallet_enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="处理钱包", variable=self._wallet_enabled_var).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=4
        )

        ttk.Label(f, text="模式").grid(row=1, column=0, sticky="w", pady=4)
        self._wallet_mode_var = tk.StringVar(value="auto")
        mode_row = ttk.Frame(f); mode_row.grid(row=1, column=1, columnspan=2, sticky="w", pady=4, padx=4)
        for val, label in [("auto", "自动导入 (推荐)"), ("manual", "人工操作后回填"), ("skip", "跳过钱包")]:
            ttk.Radiobutton(mode_row, text=label, value=val, variable=self._wallet_mode_var).pack(side=tk.LEFT, padx=4)

        ttk.Label(f, text="MetaMask 扩展 ID").grid(row=2, column=0, sticky="w", pady=4)
        self._wallet_ext_var = tk.StringVar()
        ttk.Entry(f, textvariable=self._wallet_ext_var).grid(row=2, column=1, sticky="ew", pady=4, padx=4)
        ttk.Label(f, text="(可留空自动探测)", foreground="#7f8c8d").grid(row=2, column=2, sticky="w", pady=4)

        ttk.Label(f, text="钱包密码前缀").grid(row=3, column=0, sticky="w", pady=4)
        self._wallet_pfx_var = tk.StringVar(value="Wx")
        ttk.Entry(f, textvariable=self._wallet_pfx_var, width=10).grid(row=3, column=1, sticky="w", pady=4, padx=4)

        ttk.Label(f, text="密码总长").grid(row=4, column=0, sticky="w", pady=4)
        self._wallet_plen_var = tk.StringVar(value="14")
        ttk.Entry(f, textvariable=self._wallet_plen_var, width=10).grid(row=4, column=1, sticky="w", pady=4, padx=4)

        self._wallet_fallback_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="自动失败时转人工", variable=self._wallet_fallback_var).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=4
        )

        # ---- 自动加载本地扩展（--load-extension） ----
        sep = ttk.Separator(f, orient="horizontal")
        sep.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 4))

        self._wallet_autoload_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            f,
            text="开窗口时自动加载本地 MetaMask（--load-extension，无需逐个窗口手动安装）",
            variable=self._wallet_autoload_var,
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=2)

        ttk.Label(f, text="扩展目录").grid(row=8, column=0, sticky="w", pady=4)
        self._wallet_extpath_var = tk.StringVar(value="extensions/metamask")
        path_row = ttk.Frame(f); path_row.grid(row=8, column=1, columnspan=2, sticky="ew", padx=4)
        path_row.columnconfigure(0, weight=1)
        ttk.Entry(path_row, textvariable=self._wallet_extpath_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(path_row, text="选择...", width=8, command=self._pick_extension_dir).grid(
            row=0, column=1, padx=(4, 0)
        )

        btn_row = ttk.Frame(f); btn_row.grid(row=9, column=0, columnspan=3, sticky="w", pady=(2, 0))
        ttk.Button(btn_row, text="⬇ 下载 / 安装 MetaMask", command=self._on_install_extension).pack(
            side=tk.LEFT
        )
        self._wallet_ext_status_var = tk.StringVar(value="")
        ttk.Label(btn_row, textvariable=self._wallet_ext_status_var,
                  foreground="#7f8c8d").pack(side=tk.LEFT, padx=8)

        ttk.Label(
            f,
            text=(
                "提示: 钱包数据保存在每个 ixBrowser 窗口内；扩展目录不要移动，"
                "否则各窗口里的钱包会重置。已在 ixBrowser 手动装过 MetaMask 的窗口，"
                "请关闭「自动加载」以免重复。"
            ),
            foreground="#7f8c8d", wraplength=420, justify="left",
        ).grid(row=10, column=0, columnspan=3, sticky="w", pady=(10, 0))
        return f

    def _refresh_extension_status(self) -> None:
        """根据当前路径显示扩展是否就绪。"""
        from modules import ext_install

        path = self._wallet_extpath_var.get().strip() or "extensions/metamask"
        ready, info = ext_install.extension_ready(path, base_dir=str(self._workdir))
        if not self._wallet_autoload_var.get():
            self._wallet_ext_status_var.set("自动加载已关闭")
        elif ready:
            self._wallet_ext_status_var.set(f"✓ 已就绪：{info}")
        else:
            self._wallet_ext_status_var.set("⚠ 未安装，点左侧按钮下载")

    def _pick_extension_dir(self) -> None:
        cur = self._wallet_extpath_var.get().strip() or "extensions/metamask"
        p = Path(cur)
        if not p.is_absolute():
            p = self._workdir / p
        initial = str(p) if p.exists() else (str(p.parent) if p.parent.exists() else str(self._workdir))
        chosen = filedialog.askdirectory(initialdir=initial)
        if not chosen:
            return
        try:
            rel = os.path.relpath(chosen, self._workdir)
            self._wallet_extpath_var.set(chosen if rel.startswith("..") else rel)
        except ValueError:
            self._wallet_extpath_var.set(chosen)
        self._refresh_extension_status()

    def _on_install_extension(self) -> None:
        if self._is_running:
            messagebox.showinfo("提示", "任务运行中，请先停止")
            return
        target = self._wallet_extpath_var.get().strip() or "extensions/metamask"
        self._wallet_ext_status_var.set("下载中 ...")
        self._append_log("[INFO] 开始下载 / 安装 MetaMask 扩展 ...")

        def worker() -> None:
            from modules import ext_install

            def cb(msg: str) -> None:
                self._log_queue.put(f"[EXT] {msg}")

            ok, info = ext_install.install_metamask(
                target, base_dir=str(self._workdir), cb=cb
            )
            self.after(0, lambda: self._after_install(ok, info))

        threading.Thread(target=worker, daemon=True).start()

    def _after_install(self, ok: bool, info: str) -> None:
        self._refresh_extension_status()
        if ok:
            messagebox.showinfo(
                "安装完成", f"MetaMask 已就绪：{info}\n\n下次打开窗口会自动加载。"
            )
        else:
            messagebox.showerror(
                "安装失败", info or "请检查网络，或手动把扩展解压到该目录。"
            )

    def _build_random_tab(self, parent: ttk.Widget) -> ttk.Frame:
        f = ttk.Frame(parent, padding=10)
        f.columnconfigure(1, weight=1)

        self._rnd_shuffle_rows_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            f, text="打乱待处理账号顺序", variable=self._rnd_shuffle_rows_var
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=3)

        self._rnd_shuffle_profiles_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            f, text="打乱窗口顺序（按序号指定窗口时建议关闭）",
            variable=self._rnd_shuffle_profiles_var,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=3)

        ttk.Label(f, text="账号之间随机等待").grid(row=2, column=0, sticky="w", pady=3)
        row = ttk.Frame(f); row.grid(row=2, column=1, sticky="w", pady=3, padx=4)
        self._rnd_gap_min_var = tk.StringVar(value="20")
        self._rnd_gap_max_var = tk.StringVar(value="90")
        ttk.Entry(row, textvariable=self._rnd_gap_min_var, width=6).pack(side=tk.LEFT)
        ttk.Label(row, text="~").pack(side=tk.LEFT, padx=2)
        ttk.Entry(row, textvariable=self._rnd_gap_max_var, width=6).pack(side=tk.LEFT)
        ttk.Label(row, text="秒").pack(side=tk.LEFT, padx=4)

        ttk.Label(f, text="每处理 N 个窗口后长休息").grid(row=3, column=0, sticky="w", pady=3)
        row = ttk.Frame(f); row.grid(row=3, column=1, sticky="w", pady=3, padx=4)
        self._rnd_batch_var = tk.StringVar(value="0")
        ttk.Entry(row, textvariable=self._rnd_batch_var, width=6).pack(side=tk.LEFT)
        ttk.Label(row, text="个，休息").pack(side=tk.LEFT, padx=4)
        self._rnd_rest_min_var = tk.StringVar(value="300")
        self._rnd_rest_max_var = tk.StringVar(value="900")
        ttk.Entry(row, textvariable=self._rnd_rest_min_var, width=6).pack(side=tk.LEFT)
        ttk.Label(row, text="~").pack(side=tk.LEFT, padx=2)
        ttk.Entry(row, textvariable=self._rnd_rest_max_var, width=6).pack(side=tk.LEFT)
        ttk.Label(row, text="秒").pack(side=tk.LEFT, padx=4)

        ttk.Label(f, text="本次最多处理记录数").grid(row=4, column=0, sticky="w", pady=3)
        self._rnd_limit_var = tk.StringVar(value="0")
        ttk.Entry(f, textvariable=self._rnd_limit_var, width=8).grid(
            row=4, column=1, sticky="w", pady=3, padx=4
        )
        ttk.Label(f, text="(0=不限)", foreground="#7f8c8d").grid(
            row=4, column=2, sticky="w", pady=3
        )

        self._rnd_warm_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            f, text="登录前先以访客身份浏览一会儿（更接近真人）",
            variable=self._rnd_warm_var,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=3)

        ttk.Label(f, text="访客浏览时长").grid(row=6, column=0, sticky="w", pady=3)
        row = ttk.Frame(f); row.grid(row=6, column=1, sticky="w", pady=3, padx=4)
        self._rnd_warm_min_var = tk.StringVar(value="5")
        self._rnd_warm_max_var = tk.StringVar(value="15")
        ttk.Entry(row, textvariable=self._rnd_warm_min_var, width=6).pack(side=tk.LEFT)
        ttk.Label(row, text="~").pack(side=tk.LEFT, padx=2)
        ttk.Entry(row, textvariable=self._rnd_warm_max_var, width=6).pack(side=tk.LEFT)
        ttk.Label(row, text="秒").pack(side=tk.LEFT, padx=4)

        ttk.Label(
            f,
            text="随机化只改变节奏，不改变结果。建议账号间隔 20~90 秒、每 5 个窗口休息 5~15 分钟，\n"
                 "并分多天完成，避免所有账号集中在同一时段。",
            foreground="#7f8c8d", wraplength=400, justify="left",
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(10, 0))
        return f

    def _build_warmup_tab(self, parent: ttk.Widget) -> ttk.Frame:
        f = ttk.Frame(parent, padding=10)
        f.columnconfigure(1, weight=1)

        self._wm_enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            f, text="启用养号功能", variable=self._wm_enabled_var
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=2)

        ttk.Label(f, text="登录方式").grid(row=1, column=0, sticky="w", pady=2)
        self._wm_login_mode_var = tk.StringVar(value="auto")
        mode_row = ttk.Frame(f); mode_row.grid(row=1, column=1, columnspan=3, sticky="w", pady=2)
        for value, label in [("auto", "有 token 用 token"), ("session", "只用窗口已有登录态")]:
            ttk.Radiobutton(
                mode_row, text=label, value=value, variable=self._wm_login_mode_var
            ).pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(f, text="单号停留时长").grid(row=2, column=0, sticky="w", pady=2)
        row = ttk.Frame(f); row.grid(row=2, column=1, columnspan=3, sticky="w", pady=2)
        self._wm_session_min_var = tk.StringVar(value="120")
        self._wm_session_max_var = tk.StringVar(value="300")
        ttk.Entry(row, textvariable=self._wm_session_min_var, width=6).pack(side=tk.LEFT)
        ttk.Label(row, text="~").pack(side=tk.LEFT, padx=2)
        ttk.Entry(row, textvariable=self._wm_session_max_var, width=6).pack(side=tk.LEFT)
        ttk.Label(row, text="秒").pack(side=tk.LEFT, padx=4)

        ttk.Label(f, text="本次养号账号数").grid(row=3, column=0, sticky="w", pady=2)
        row = ttk.Frame(f); row.grid(row=3, column=1, columnspan=3, sticky="w", pady=2)
        self._wm_accounts_var = tk.StringVar(value="0")
        ttk.Entry(row, textvariable=self._wm_accounts_var, width=6).pack(side=tk.LEFT)
        ttk.Label(row, text="(0=全部)   每号每天").pack(side=tk.LEFT, padx=4)
        self._wm_daily_var = tk.StringVar(value="1")
        ttk.Entry(row, textvariable=self._wm_daily_var, width=4).pack(side=tk.LEFT)
        ttk.Label(row, text="次").pack(side=tk.LEFT, padx=2)

        ttk.Label(f, text="滚动后停留").grid(row=4, column=0, sticky="w", pady=2)
        row = ttk.Frame(f); row.grid(row=4, column=1, columnspan=3, sticky="w", pady=2)
        self._wm_scroll_min_var = tk.StringVar(value="2.0")
        self._wm_scroll_max_var = tk.StringVar(value="7.0")
        ttk.Entry(row, textvariable=self._wm_scroll_min_var, width=6).pack(side=tk.LEFT)
        ttk.Label(row, text="~").pack(side=tk.LEFT, padx=2)
        ttk.Entry(row, textvariable=self._wm_scroll_max_var, width=6).pack(side=tk.LEFT)
        ttk.Label(row, text="秒   最多滚动").pack(side=tk.LEFT, padx=4)
        self._wm_max_scrolls_var = tk.StringVar(value="20")
        ttk.Entry(row, textvariable=self._wm_max_scrolls_var, width=5).pack(side=tk.LEFT)
        ttk.Label(row, text="次").pack(side=tk.LEFT, padx=2)

        ttk.Label(f, text="点赞概率 / 上限").grid(row=5, column=0, sticky="w", pady=2)
        row = ttk.Frame(f); row.grid(row=5, column=1, columnspan=3, sticky="w", pady=2)
        self._wm_like_prob_var = tk.StringVar(value="25")
        self._wm_max_likes_var = tk.StringVar(value="6")
        ttk.Entry(row, textvariable=self._wm_like_prob_var, width=5).pack(side=tk.LEFT)
        ttk.Label(row, text="%  最多").pack(side=tk.LEFT, padx=2)
        ttk.Entry(row, textvariable=self._wm_max_likes_var, width=5).pack(side=tk.LEFT)
        ttk.Label(row, text="个").pack(side=tk.LEFT, padx=2)

        ttk.Label(f, text="转推概率 / 上限").grid(row=6, column=0, sticky="w", pady=2)
        row = ttk.Frame(f); row.grid(row=6, column=1, columnspan=3, sticky="w", pady=2)
        self._wm_rt_prob_var = tk.StringVar(value="3")
        self._wm_max_rt_var = tk.StringVar(value="2")
        ttk.Entry(row, textvariable=self._wm_rt_prob_var, width=5).pack(side=tk.LEFT)
        ttk.Label(row, text="%  最多").pack(side=tk.LEFT, padx=2)
        ttk.Entry(row, textvariable=self._wm_max_rt_var, width=5).pack(side=tk.LEFT)
        ttk.Label(row, text="个").pack(side=tk.LEFT, padx=2)

        ttk.Label(f, text="关注概率 / 上限").grid(row=7, column=0, sticky="w", pady=2)
        row = ttk.Frame(f); row.grid(row=7, column=1, columnspan=3, sticky="w", pady=2)
        self._wm_follow_prob_var = tk.StringVar(value="4")
        self._wm_max_follow_var = tk.StringVar(value="1")
        ttk.Entry(row, textvariable=self._wm_follow_prob_var, width=5).pack(side=tk.LEFT)
        ttk.Label(row, text="%  最多").pack(side=tk.LEFT, padx=2)
        ttk.Entry(row, textvariable=self._wm_max_follow_var, width=5).pack(side=tk.LEFT)
        ttk.Label(row, text="个").pack(side=tk.LEFT, padx=2)

        ttk.Label(f, text="其他动作概率").grid(row=8, column=0, sticky="w", pady=2)
        row = ttk.Frame(f); row.grid(row=8, column=1, columnspan=3, sticky="w", pady=2)
        self._wm_video_prob_var = tk.StringVar(value="12")
        self._wm_search_prob_var = tk.StringVar(value="30")
        self._wm_profile_prob_var = tk.StringVar(value="25")
        ttk.Label(row, text="视频").pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self._wm_video_prob_var, width=4).pack(side=tk.LEFT, padx=2)
        ttk.Label(row, text="%  搜索").pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self._wm_search_prob_var, width=4).pack(side=tk.LEFT, padx=2)
        ttk.Label(row, text="%  主页").pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self._wm_profile_prob_var, width=4).pack(side=tk.LEFT, padx=2)
        ttk.Label(row, text="%").pack(side=tk.LEFT)

        ttk.Label(f, text="搜索关键词").grid(row=9, column=0, sticky="w", pady=2)
        self._wm_keywords_var = tk.StringVar()
        ttk.Entry(f, textvariable=self._wm_keywords_var).grid(
            row=9, column=1, columnspan=3, sticky="ew", pady=2, padx=4
        )

        ttk.Label(f, text="允许运行时段").grid(row=10, column=0, sticky="w", pady=2)
        row = ttk.Frame(f); row.grid(row=10, column=1, columnspan=3, sticky="w", pady=2)
        self._wm_hours_var = tk.StringVar()
        ttk.Entry(row, textvariable=self._wm_hours_var, width=8).pack(side=tk.LEFT)
        ttk.Label(row, text="(如 9-23，留空=不限)   账号间隔休息").pack(side=tk.LEFT, padx=4)
        self._wm_rest_min_var = tk.StringVar(value="60")
        self._wm_rest_max_var = tk.StringVar(value="240")
        ttk.Entry(row, textvariable=self._wm_rest_min_var, width=5).pack(side=tk.LEFT)
        ttk.Label(row, text="~").pack(side=tk.LEFT, padx=2)
        ttk.Entry(row, textvariable=self._wm_rest_max_var, width=5).pack(side=tk.LEFT)
        ttk.Label(row, text="秒").pack(side=tk.LEFT, padx=2)

        self._warmup_btn = ttk.Button(
            f, text="🌱 开始养号", command=self._on_start_warmup
        )
        self._warmup_btn.grid(row=11, column=0, columnspan=4, sticky="w", pady=(10, 2))

        ttk.Label(
            f,
            text="养号只做浏览、点赞、转推、收藏、关注、搜索等轻量动作，不会发文或改资料。\n"
                 "建议每天固定时段跑一次，配合 ixBrowser 窗口各自的代理 IP 使用。",
            foreground="#7f8c8d", wraplength=420, justify="left",
        ).grid(row=12, column=0, columnspan=4, sticky="w", pady=(6, 0))
        return f

    # -------------------------------------------------------- 记录查询页签
    def _build_records_tab(self, parent: ttk.Widget) -> ttk.Frame:
        f = ttk.Frame(parent, padding=10)

        top = ttk.Frame(f)
        top.pack(fill=tk.X)
        ttk.Label(top, text="搜索").pack(side=tk.LEFT)
        self._rec_kw_var = tk.StringVar()
        kw_entry = ttk.Entry(top, textvariable=self._rec_kw_var, width=20)
        kw_entry.pack(side=tk.LEFT, padx=4)
        kw_entry.bind("<Return>", lambda _event: self._refresh_records())

        ttk.Label(top, text="最终结果").pack(side=tk.LEFT, padx=(8, 0))
        self._rec_status_var = tk.StringVar(value="全部")
        status_box = ttk.Combobox(
            top,
            textvariable=self._rec_status_var,
            width=12,
            state="readonly",
            values=["全部", "已登录", "全部失败", "受限/需人工", "异常", "未处理"],
        )
        status_box.pack(side=tk.LEFT, padx=4)
        status_box.bind("<<ComboboxSelected>>", lambda _event: self._refresh_records())

        ttk.Button(top, text="查询", command=self._refresh_records).pack(side=tk.LEFT, padx=4)

        date_row = ttk.Frame(f)
        date_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(date_row, text="日期范围").pack(side=tk.LEFT)
        self._rec_from_var = tk.StringVar()
        ttk.Entry(date_row, textvariable=self._rec_from_var, width=12).pack(side=tk.LEFT, padx=4)
        ttk.Label(date_row, text="~").pack(side=tk.LEFT)
        self._rec_to_var = tk.StringVar()
        ttk.Entry(date_row, textvariable=self._rec_to_var, width=12).pack(side=tk.LEFT, padx=4)
        ttk.Label(date_row, text="(YYYY-MM-DD，可空)", foreground="#7f8c8d").pack(side=tk.LEFT, padx=4)
        ttk.Button(date_row, text="导出 Excel", command=self._on_export_records).pack(
            side=tk.LEFT, padx=(12, 0)
        )

        self._rec_summary_var = tk.StringVar(value="暂无记录")
        ttk.Label(f, textvariable=self._rec_summary_var, foreground="#7f8c8d").pack(
            anchor="w", pady=(8, 4)
        )

        columns = ("idx", "account", "token", "password", "final", "profile", "time", "note")
        headings = {
            "idx": "序号",
            "account": "账号",
            "token": "token登录",
            "password": "账号登录",
            "final": "最终结果",
            "profile": "窗口",
            "time": "更新时间",
            "note": "备注",
        }
        widths = {
            "idx": 50,
            "account": 110,
            "token": 80,
            "password": 80,
            "final": 90,
            "profile": 100,
            "time": 135,
            "note": 220,
        }

        wrap = ttk.Frame(f)
        wrap.pack(fill=tk.BOTH, expand=True)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self._rec_tree = ttk.Treeview(wrap, columns=columns, show="headings", height=16)
        for name in columns:
            self._rec_tree.heading(name, text=headings[name])
            self._rec_tree.column(
                name, width=widths[name], anchor="w", stretch=(name == "note")
            )
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self._rec_tree.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self._rec_tree.xview)
        self._rec_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._rec_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self._rec_tree.tag_configure("ok", foreground="#1e8449")
        self._rec_tree.tag_configure("fail", foreground="#c0392b")
        self._rec_tree.tag_configure("warn", foreground="#b9770e")
        self._rec_tree.bind("<Double-1>", self._on_record_open)
        return f

    # --------------------------------------------------- 设置 <-> UI
    def _load_settings_to_ui(self) -> None:
        s = self._settings
        self._excel_var.set(str(Path(s.excel.path)))
        self._sheet_var.set(s.excel.sheet)
        self._token_col_var.set(s.excel.token_column)
        self._ct0_col_var.set(s.excel.ct0_column)
        self._user_col_var.set(s.excel.username_column)
        self._pwd_col_var.set(s.excel.password_column)
        self._twofa_col_var.set(s.excel.twofa_column)
        self._backup_var.set(s.excel.backup)
        self._excel_row_start_var.set(str(s.excel.row_start))
        self._excel_row_end_var.set(str(s.excel.row_end))
        self._excel_retry_failed_var.set(s.excel.retry_failed_only)

        self._ix_host_var.set(s.ixbrowser.host)
        self._ix_port_var.set(str(s.ixbrowser.port))
        self._ix_filter_var.set(s.ixbrowser.profile_name_filter)
        self._ix_limit_var.set(str(s.ixbrowser.profile_limit))
        self._ix_start_var.set(str(s.ixbrowser.profile_start))
        self._ix_end_var.set(str(s.ixbrowser.profile_end))
        self._keep_open_var.set(s.ixbrowser.keep_window_open)

        self._tw_timeout_var.set(str(s.twitter.login_timeout))
        self._tw_max_var.set(str(s.twitter.max_tokens_per_profile))
        self._tw_retry_var.set(str(s.twitter.retry_per_token))
        self._tw_delay_min_var.set(str(s.twitter.delay_min))
        self._tw_delay_max_var.set(str(s.twitter.delay_max))
        self._tw_pwd_login_var.set(s.twitter.password_login)
        self._tw_pwd_timeout_var.set(str(s.twitter.password_timeout))

        self._wallet_enabled_var.set(s.wallet.enabled)
        self._wallet_mode_var.set(s.wallet.mode)
        self._wallet_ext_var.set(s.wallet.extension_id)
        self._wallet_autoload_var.set(s.wallet.auto_load_extension)
        self._wallet_extpath_var.set(s.wallet.extension_path or "extensions/metamask")
        self._wallet_pfx_var.set(s.wallet.password_prefix)
        self._wallet_plen_var.set(str(s.wallet.password_length))
        self._wallet_fallback_var.set(s.wallet.manual_fallback)
        self._refresh_extension_status()
        self._on_pwd_login_toggle()

        r = s.randomize
        self._rnd_shuffle_rows_var.set(r.shuffle_rows)
        self._rnd_shuffle_profiles_var.set(r.shuffle_profiles)
        self._rnd_gap_min_var.set(str(r.account_delay_min))
        self._rnd_gap_max_var.set(str(r.account_delay_max))
        self._rnd_batch_var.set(str(r.batch_size))
        self._rnd_rest_min_var.set(str(r.batch_rest_min))
        self._rnd_rest_max_var.set(str(r.batch_rest_max))
        self._rnd_limit_var.set(str(r.daily_limit))
        self._rnd_warm_var.set(r.warm_before_login)
        self._rnd_warm_min_var.set(str(r.warm_seconds_min))
        self._rnd_warm_max_var.set(str(r.warm_seconds_max))

        w = s.warmup
        self._wm_enabled_var.set(w.enabled)
        self._wm_login_mode_var.set(w.login_mode)
        self._wm_session_min_var.set(str(w.session_min))
        self._wm_session_max_var.set(str(w.session_max))
        self._wm_accounts_var.set(str(w.accounts_per_run))
        self._wm_daily_var.set(str(w.daily_per_account))
        self._wm_scroll_min_var.set(str(w.scroll_min))
        self._wm_scroll_max_var.set(str(w.scroll_max))
        self._wm_max_scrolls_var.set(str(w.max_scrolls))
        self._wm_like_prob_var.set(str(int(w.like_prob * 100)))
        self._wm_max_likes_var.set(str(w.max_likes))
        self._wm_rt_prob_var.set(str(int(w.retweet_prob * 100)))
        self._wm_max_rt_var.set(str(w.max_retweets))
        self._wm_follow_prob_var.set(str(int(w.follow_prob * 100)))
        self._wm_max_follow_var.set(str(w.max_follows))
        self._wm_video_prob_var.set(str(int(w.video_prob * 100)))
        self._wm_search_prob_var.set(str(int(w.search_prob * 100)))
        self._wm_profile_prob_var.set(str(int(w.profile_prob * 100)))
        self._wm_keywords_var.set("，".join(w.keywords))
        self._wm_hours_var.set(w.active_hours)
        self._wm_rest_min_var.set(str(w.rest_min))
        self._wm_rest_max_var.set(str(w.rest_max))

    def _on_pwd_login_toggle(self) -> None:
        """账号密码登录关闭时，把相关列名输入框置灰，避免误解为必填。"""
        enabled = bool(self._tw_pwd_login_var.get())
        state = "normal" if enabled else "disabled"
        for widget in (self._user_entry, self._pwd_entry, self._twofa_entry):
            widget.configure(state=state)
        self._pwd_hint_var.set(
            "已启用账号密码登录：需要 账号 / 密码 / 2FA 三列"
            if enabled
            else "已关闭账号密码登录：只需 token 一列即可运行"
        )

    def _collect_settings(self) -> Settings:
        """从 GUI 各变量构建一个新的 Settings，保留其他未暴露字段（output.home_url 等）。"""
        s = self._settings

        def _int(var: tk.StringVar, default: int) -> int:
            try:
                return int(var.get().strip())
            except Exception:
                return default

        def _float(var: tk.StringVar, default: float) -> float:
            try:
                return float(var.get().strip())
            except Exception:
                return default

        def _pct(var: tk.StringVar, default: float) -> float:
            """把界面上的百分比（0~100）转换成 0~1 的概率并夹紧。"""
            return min(1.0, max(0.0, _float(var, default) / 100.0))

        s.excel.path = self._excel_var.get().strip() or "accounts.xlsx"
        s.excel.sheet = self._sheet_var.get().strip()
        s.excel.token_column = self._token_col_var.get().strip() or "auth_token"
        s.excel.ct0_column = self._ct0_col_var.get().strip()
        s.excel.username_column = self._user_col_var.get().strip() or "账号"
        s.excel.password_column = self._pwd_col_var.get().strip() or "密码"
        s.excel.twofa_column = self._twofa_col_var.get().strip() or "2FA"
        s.excel.backup = bool(self._backup_var.get())
        s.excel.row_start = max(0, _int(self._excel_row_start_var, 0))
        s.excel.row_end = max(0, _int(self._excel_row_end_var, 0))
        s.excel.retry_failed_only = bool(self._excel_retry_failed_var.get())

        s.ixbrowser.host = self._ix_host_var.get().strip() or "127.0.0.1"
        s.ixbrowser.port = _int(self._ix_port_var, 53200)
        s.ixbrowser.profile_name_filter = self._ix_filter_var.get().strip()
        s.ixbrowser.profile_limit = max(0, _int(self._ix_limit_var, 1))
        s.ixbrowser.profile_start = max(1, _int(self._ix_start_var, 1))
        end_value = max(0, _int(self._ix_end_var, 0))
        # 指定结束序号时不能小于起始序号
        if end_value > 0 and end_value < s.ixbrowser.profile_start:
            end_value = s.ixbrowser.profile_start
        s.ixbrowser.profile_end = end_value
        s.ixbrowser.keep_window_open = bool(self._keep_open_var.get())

        s.twitter.login_timeout = max(5, _int(self._tw_timeout_var, 25))
        s.twitter.max_tokens_per_profile = max(1, _int(self._tw_max_var, 20))
        s.twitter.retry_per_token = max(0, _int(self._tw_retry_var, 1))
        s.twitter.delay_min = max(0.0, _float(self._tw_delay_min_var, 3.0))
        s.twitter.delay_max = max(0.0, _float(self._tw_delay_max_var, 6.0))
        s.twitter.password_login = bool(self._tw_pwd_login_var.get())
        s.twitter.password_timeout = max(10, _int(self._tw_pwd_timeout_var, 40))

        s.wallet.enabled = bool(self._wallet_enabled_var.get())
        s.wallet.mode = self._wallet_mode_var.get().strip() or "auto"
        s.wallet.extension_id = self._wallet_ext_var.get().strip()
        s.wallet.auto_load_extension = bool(self._wallet_autoload_var.get())
        s.wallet.extension_path = self._wallet_extpath_var.get().strip() or "extensions/metamask"
        s.wallet.password_prefix = self._wallet_pfx_var.get().strip() or "Wx"
        s.wallet.password_length = max(
            len(s.wallet.password_prefix) + 5, _int(self._wallet_plen_var, 14)
        )
        s.wallet.manual_fallback = bool(self._wallet_fallback_var.get())

        s.randomize.shuffle_rows = bool(self._rnd_shuffle_rows_var.get())
        s.randomize.shuffle_profiles = bool(self._rnd_shuffle_profiles_var.get())
        s.randomize.account_delay_min = max(0.0, _float(self._rnd_gap_min_var, 20.0))
        s.randomize.account_delay_max = max(0.0, _float(self._rnd_gap_max_var, 90.0))
        s.randomize.batch_size = max(0, _int(self._rnd_batch_var, 0))
        s.randomize.batch_rest_min = max(0.0, _float(self._rnd_rest_min_var, 300.0))
        s.randomize.batch_rest_max = max(0.0, _float(self._rnd_rest_max_var, 900.0))
        s.randomize.daily_limit = max(0, _int(self._rnd_limit_var, 0))
        s.randomize.warm_before_login = bool(self._rnd_warm_var.get())
        s.randomize.warm_seconds_min = max(1, _int(self._rnd_warm_min_var, 5))
        s.randomize.warm_seconds_max = max(1, _int(self._rnd_warm_max_var, 15))

        w = s.warmup
        w.enabled = bool(self._wm_enabled_var.get())
        w.login_mode = self._wm_login_mode_var.get().strip() or "auto"
        w.session_min = max(10, _int(self._wm_session_min_var, 120))
        w.session_max = max(w.session_min, _int(self._wm_session_max_var, 300))
        w.accounts_per_run = max(0, _int(self._wm_accounts_var, 0))
        w.daily_per_account = max(1, _int(self._wm_daily_var, 1))
        w.scroll_min = max(0.0, _float(self._wm_scroll_min_var, 2.0))
        w.scroll_max = max(0.0, _float(self._wm_scroll_max_var, 7.0))
        w.max_scrolls = max(1, _int(self._wm_max_scrolls_var, 20))
        w.like_prob = _pct(self._wm_like_prob_var, 25)
        w.max_likes = max(0, _int(self._wm_max_likes_var, 6))
        w.retweet_prob = _pct(self._wm_rt_prob_var, 3)
        w.max_retweets = max(0, _int(self._wm_max_rt_var, 2))
        w.follow_prob = _pct(self._wm_follow_prob_var, 4)
        w.max_follows = max(0, _int(self._wm_max_follow_var, 1))
        w.video_prob = _pct(self._wm_video_prob_var, 12)
        w.search_prob = _pct(self._wm_search_prob_var, 30)
        w.profile_prob = _pct(self._wm_profile_prob_var, 25)
        raw_keywords = self._wm_keywords_var.get().replace("，", ",")
        w.keywords = [k.strip() for k in raw_keywords.split(",") if k.strip()]
        w.active_hours = self._wm_hours_var.get().strip()
        w.rest_min = max(0, _int(self._wm_rest_min_var, 60))
        w.rest_max = max(w.rest_min, _int(self._wm_rest_max_var, 240))

        return s

    # -------------------------------------------------------------- 按钮
    def _pick_excel(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 token 表",
            filetypes=[("Excel", "*.xlsx *.xls"), ("所有文件", "*.*")],
        )
        if path:
            self._excel_var.set(path)

    def _on_download_template(self) -> None:
        """生成默认格式的登录表（表头与当前界面的列名一致）。"""
        path = filedialog.asksaveasfilename(
            title="保存登录模版",
            defaultextension=".xlsx",
            initialfile="登录模版.xlsx",
            initialdir=str(self._workdir),
            filetypes=[("Excel", "*.xlsx")],
        )
        if not path:
            return
        try:
            from modules.excel_store import create_template

            dest = create_template(
                path,
                token_column=self._token_col_var.get().strip() or "auth_token",
                ct0_column=self._ct0_col_var.get().strip(),
                username_column=self._user_col_var.get().strip() or "账号",
                password_column=self._pwd_col_var.get().strip() or "密码",
                twofa_column=self._twofa_col_var.get().strip() or "2FA",
            )
        except Exception as exc:
            messagebox.showerror("生成失败", str(exc))
            return
        self._append_log(f"[INFO] 已生成登录模版: {dest}")
        messagebox.showinfo(
            "模版已生成",
            f"已保存到:\n{dest}\n\n填好 token / 账号 / 密码 / 2FA 后，"
            "在上方「token 表路径」选择它即可。",
        )

    def _on_download_result(self) -> None:
        """把执行结果表另存一份副本，原表保持不变。"""
        if self._is_running:
            messagebox.showinfo("提示", "任务运行中，结果表可能正在写入，请先停止再下载")
            return
        configured = self._excel_var.get().strip()
        if not configured:
            messagebox.showerror("提示", "请先选择 token 表路径")
            return
        try:
            from modules.excel_store import export_copy, resolve_result_path

            source = resolve_result_path(configured)
        except Exception as exc:
            messagebox.showerror("找不到结果表", str(exc))
            return

        path = filedialog.asksaveasfilename(
            title="另存执行结果",
            defaultextension=".xlsx",
            initialfile=f"{source.stem}_结果{source.suffix}",
            initialdir=str(source.parent),
            filetypes=[("Excel", "*.xlsx")],
        )
        if not path:
            return
        try:
            dest = export_copy(source, path)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self._append_log(f"[INFO] 执行结果已另存: {source} -> {dest}")
        messagebox.showinfo("已另存", f"结果已保存到:\n{dest}\n\n原表未改动。")

    def _on_reset_failed(self) -> None:
        """清空失败/受限/异常行的状态列，便于下次重跑。"""
        if self._is_running:
            messagebox.showinfo("提示", "任务运行中，请先停止")
            return
        s = self._collect_settings()
        if not Path(s.excel.path).exists():
            messagebox.showerror("参数错误", f"Excel 不存在:\n{s.excel.path}")
            return
        if not messagebox.askyesno(
            "确认重置",
            "将清空「全部失败 / 受限 / 异常」行的 状态、最终结果、token登录、账号登录 列。\n"
            "原始 token / 账号 / 密码不受影响。是否继续？",
        ):
            return
        try:
            from modules.excel_store import ExcelStore

            store = ExcelStore(s.excel)
            count = store.reset_failed()
        except Exception as exc:
            messagebox.showerror("重置失败", str(exc))
            return
        messagebox.showinfo("已完成", f"已重置 {count} 行失败记录")
        self._refresh_records()

    def _validate_settings(self, s: Settings) -> bool:
        """开始前的参数校验；不合法时弹窗并返回 False。"""
        problems = []
        if s.twitter.delay_min > s.twitter.delay_max:
            problems.append("Twitter 动作延迟：最小值不能大于最大值")
        if s.randomize.account_delay_min > s.randomize.account_delay_max:
            problems.append("随机化：账号间隔最小值不能大于最大值")
        if s.randomize.batch_rest_min > s.randomize.batch_rest_max:
            problems.append("随机化：长休息最小值不能大于最大值")
        if s.randomize.warm_seconds_min > s.randomize.warm_seconds_max:
            problems.append("随机化：访客浏览时长最小值不能大于最大值")
        if s.warmup.scroll_min > s.warmup.scroll_max:
            problems.append("养号：滚动停留最小值不能大于最大值")
        if s.warmup.session_min > s.warmup.session_max:
            problems.append("养号：单号停留时长最小值不能大于最大值")
        min_len = len(s.wallet.password_prefix) + 5
        if s.wallet.password_length < min_len:
            problems.append(f"钱包密码总长至少为 前缀长度+5（{min_len}）")
        if problems:
            messagebox.showerror("参数错误", "\n".join(problems))
            return False
        return True

    def _test_ix(self) -> None:
        try:
            s = self._collect_settings()
            from modules.browser import BrowserManager
            bm = BrowserManager(s.ixbrowser)
            profiles = bm.list_profiles()
            lines = [
                f"已连接 {s.ixbrowser.host}:{s.ixbrowser.port}，共 {len(profiles)} 个窗口",
                "",
            ]
            for index, item in enumerate(profiles[:20], start=1):
                lines.append(f"[{index}] {item['name'] or '(未命名)'}  id={item['id']}")
            if len(profiles) > 20:
                lines.append(f"... 其余 {len(profiles) - 20} 个省略")
            messagebox.showinfo("ixBrowser 连接成功", "\n".join(lines))
        except Exception as exc:
            messagebox.showerror("连接失败", str(exc))

    def _on_save_cfg(self) -> None:
        if self._is_running:
            messagebox.showinfo("提示", "任务运行中，配置参数已锁定，请先停止再保存")
            return
        self._save_config(silent=False)

    def _save_config(self, silent: bool = False) -> bool:
        s = self._collect_settings()
        try:
            import yaml
        except ImportError:  # pragma: no cover
            if not silent:
                messagebox.showerror("缺少依赖", "请先执行: pip install PyYAML")
            return False
        try:
            data = {
                "excel": {
                    "path": s.excel.path,
                    "sheet": s.excel.sheet,
                    "token_column": s.excel.token_column,
                    "ct0_column": s.excel.ct0_column,
                    "username_column": s.excel.username_column,
                    "password_column": s.excel.password_column,
                    "twofa_column": s.excel.twofa_column,
                    "backup": s.excel.backup,
                    "row_start": s.excel.row_start,
                    "row_end": s.excel.row_end,
                    "retry_failed_only": s.excel.retry_failed_only,
                },
                "ixbrowser": {
                    "host": s.ixbrowser.host, "port": s.ixbrowser.port,
                    "profile_name_filter": s.ixbrowser.profile_name_filter,
                    "profile_start": s.ixbrowser.profile_start,
                    "profile_end": s.ixbrowser.profile_end,
                    "profile_limit": s.ixbrowser.profile_limit,
                    "keep_window_open": s.ixbrowser.keep_window_open,
                },
                "twitter": {
                    "home_url": s.twitter.home_url, "login_url": s.twitter.login_url,
                    "login_timeout": s.twitter.login_timeout,
                    "password_login": s.twitter.password_login,
                    "password_timeout": s.twitter.password_timeout,
                    "max_tokens_per_profile": s.twitter.max_tokens_per_profile,
                    "retry_per_token": s.twitter.retry_per_token,
                    "delay_min": s.twitter.delay_min, "delay_max": s.twitter.delay_max,
                },
                "wallet": {
                    "enabled": s.wallet.enabled, "mode": s.wallet.mode,
                    "extension_id": s.wallet.extension_id,
                    "auto_load_extension": s.wallet.auto_load_extension,
                    "extension_path": s.wallet.extension_path,
                    "password_prefix": s.wallet.password_prefix,
                    "password_length": s.wallet.password_length,
                    "step_timeout": s.wallet.step_timeout,
                    "manual_fallback": s.wallet.manual_fallback,
                },
                "output": {
                    "log_dir": s.output.log_dir,
                    "screenshot_dir": s.output.screenshot_dir,
                    "state_db": s.output.state_db,
                },
                "randomize": {
                    "shuffle_rows": s.randomize.shuffle_rows,
                    "shuffle_profiles": s.randomize.shuffle_profiles,
                    "account_delay_min": s.randomize.account_delay_min,
                    "account_delay_max": s.randomize.account_delay_max,
                    "batch_size": s.randomize.batch_size,
                    "batch_rest_min": s.randomize.batch_rest_min,
                    "batch_rest_max": s.randomize.batch_rest_max,
                    "daily_limit": s.randomize.daily_limit,
                    "warm_before_login": s.randomize.warm_before_login,
                    "warm_seconds_min": s.randomize.warm_seconds_min,
                    "warm_seconds_max": s.randomize.warm_seconds_max,
                },
                "warmup": {
                    "enabled": s.warmup.enabled,
                    "login_mode": s.warmup.login_mode,
                    "session_min": s.warmup.session_min,
                    "session_max": s.warmup.session_max,
                    "scroll_min": s.warmup.scroll_min,
                    "scroll_max": s.warmup.scroll_max,
                    "max_scrolls": s.warmup.max_scrolls,
                    "like_prob": s.warmup.like_prob,
                    "max_likes": s.warmup.max_likes,
                    "retweet_prob": s.warmup.retweet_prob,
                    "max_retweets": s.warmup.max_retweets,
                    "bookmark_prob": s.warmup.bookmark_prob,
                    "max_bookmarks": s.warmup.max_bookmarks,
                    "follow_prob": s.warmup.follow_prob,
                    "max_follows": s.warmup.max_follows,
                    "video_prob": s.warmup.video_prob,
                    "search_prob": s.warmup.search_prob,
                    "profile_prob": s.warmup.profile_prob,
                    "keywords": s.warmup.keywords,
                    "accounts_per_run": s.warmup.accounts_per_run,
                    "daily_per_account": s.warmup.daily_per_account,
                    "active_hours": s.warmup.active_hours,
                    "rest_min": s.warmup.rest_min,
                    "rest_max": s.warmup.rest_max,
                },
            }
            with open(self._config_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
            if not silent:
                messagebox.showinfo("已保存", str(self._config_path))
            return True
        except Exception as exc:
            if not silent:
                messagebox.showerror("保存失败", str(exc))
            return False

    def _on_load_cfg(self) -> None:
        if self._is_running:
            messagebox.showinfo("提示", "任务运行中，请先停止再加载配置")
            return
        path = filedialog.askopenfilename(
            title="选择配置文件",
            filetypes=[("YAML", "*.yaml *.yml"), ("所有文件", "*.*")],
            initialdir=str(self._workdir),
        )
        if not path:
            return
        try:
            self._settings = load_settings(path)
            self._load_settings_to_ui()
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc))

    # ----------------------------------------------------------- 记录查询
    def _get_db(self, path: str):
        """按路径缓存 StateDB 实例，避免每次查询都新建连接。"""
        from modules.state_db import StateDB

        resolved = str(Path(path).resolve())
        if self._db is None or self._db_path != resolved:
            self._db = StateDB(resolved)
            self._db_path = resolved
        return self._db

    def _refresh_records(self) -> None:
        try:
            s = self._collect_settings()
            db = self._get_db(s.output.state_db)
            status = self._rec_status_var.get().strip()
            rows = db.query(
                self._rec_kw_var.get().strip(),
                "" if status == "全部" else status,
                date_from=self._rec_from_var.get().strip(),
                date_to=self._rec_to_var.get().strip(),
            )
        except Exception as exc:
            self._rec_summary_var.set(f"读取记录失败: {exc}")
            return
        self._rec_rows = rows

        for item in self._rec_tree.get_children():
            self._rec_tree.delete(item)

        for record in rows:
            final = record.get("final_status") or "未处理"
            if final == "已登录":
                tag = "ok"
            elif final in ("受限/需人工", "异常"):
                tag = "warn"
            else:
                tag = "fail"
            self._rec_tree.insert(
                "",
                tk.END,
                values=(
                    record.get("idx") or "",
                    record.get("account") or "",
                    record.get("token_status") or "未尝试",
                    record.get("password_status") or "未尝试",
                    final,
                    record.get("profile") or "",
                    record.get("updated_at") or "",
                    record.get("note") or "",
                ),
                tags=(tag,),
            )

        summary = db.summary()
        if summary:
            text = "  |  ".join(f"{name}: {count}" for name, count in summary.items())
        else:
            text = "暂无记录"
        self._rec_summary_var.set(text)

    def _on_export_records(self) -> None:
        """把当前查询结果导出为 Excel。"""
        if not self._rec_rows:
            messagebox.showinfo("提示", "当前没有可导出的记录，请先查询")
            return
        path = filedialog.asksaveasfilename(
            title="导出记录",
            defaultextension=".xlsx",
            initialfile="记录导出.xlsx",
            initialdir=str(self._workdir),
            filetypes=[("Excel", "*.xlsx")],
        )
        if not path:
            return
        try:
            from openpyxl import Workbook

            wb = Workbook()
            ws = wb.active
            ws.title = "记录"
            ws.append(["序号", "账号", "token登录", "账号登录", "最终结果",
                       "窗口", "更新时间", "备注"])
            for record in self._rec_rows:
                ws.append([
                    record.get("idx") or "",
                    record.get("account") or "",
                    record.get("token_status") or "未尝试",
                    record.get("password_status") or "未尝试",
                    record.get("final_status") or "未处理",
                    record.get("profile") or "",
                    record.get("updated_at") or "",
                    record.get("note") or "",
                ])
            wb.save(path)
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))
            return
        messagebox.showinfo("已导出", path)

    def _on_record_open(self, _event=None) -> None:
        selection = self._rec_tree.selection()
        if not selection:
            return
        values = self._rec_tree.item(selection[0], "values")
        profile = values[5] if len(values) > 5 else ""
        self._open_wallet_screenshot(profile)

    def _open_wallet_screenshot(self, profile: str) -> None:
        """双击记录时打开该窗口最新的钱包失败截图，找不到则打开截图目录。"""
        s = self._collect_settings()
        shot_dir = self._resolve_dir(s.output.screenshot_dir)
        safe = re.sub(r"[^\w\-]+", "_", profile or "")[:40]
        candidates = []
        if safe:
            try:
                candidates = sorted(
                    shot_dir.glob(f"wallet_fail_*{safe}*.png"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
            except Exception:
                candidates = []
        if candidates:
            self._open_path(candidates[0])
            return
        self._open_path(shot_dir)

    # ----------------------------------------------------------- 运行控制
    def _on_start(self) -> None:
        if self._is_running:
            return
        s = self._collect_settings()

        # 基础校验
        if not Path(s.excel.path).exists():
            messagebox.showerror("参数错误", f"Excel 不存在:\n{s.excel.path}")
            return
        if not self._validate_settings(s):
            return

        if not s.wallet.enabled and s.wallet.mode != "skip":
            # 让 GUI 上 radio 覆盖
            pass

        # 钱包模式下若启用了自动加载但扩展未就绪，提示是否仍要继续
        if (
            s.wallet.enabled
            and s.wallet.mode != "skip"
            and s.wallet.auto_load_extension
        ):
            from modules import ext_install

            ready, _ = ext_install.extension_ready(
                s.wallet.extension_path, base_dir=str(self._workdir)
            )
            if not ready:
                if not messagebox.askyesno(
                    "MetaMask 未安装",
                    "未检测到本地 MetaMask 扩展，本次将无法初始化钱包。\n"
                    "是否仍要继续（仅登录 Twitter）？",
                ):
                    return

        self._launch("login", s)

    def _on_start_warmup(self) -> None:
        if self._is_running:
            messagebox.showinfo("提示", "已有任务在运行，请先停止")
            return
        s = self._collect_settings()
        if not Path(s.excel.path).exists():
            messagebox.showerror("参数错误", f"Excel 不存在:\n{s.excel.path}")
            return
        if not s.warmup.enabled:
            messagebox.showerror("参数错误", "养号功能未启用（⑥ 养号页签）")
            return
        if not self._validate_settings(s):
            return
        self._launch("warmup", s)

    def _launch(self, mode: str, s: Settings) -> None:
        # 界面上的参数直接写回 config.yaml，避免还要手动改文件
        self._save_config(silent=True)

        self._reset_log()
        self._reset_stats()

        self._task_mode = mode
        self._stop_event = threading.Event()
        self._log_queue = queue.Queue()
        self._is_running = True
        self._set_running_state(True)

        try:
            self._worker = threading.Thread(
                target=self._run_worker, args=(s, mode), daemon=True
            )
            self._worker.start()
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))
            self._set_running_state(False)

    def _on_stop(self) -> None:
        if not self._is_running:
            return
        if not messagebox.askyesno("确认", "请求停止？当前窗口处理完即退出。"):
            return
        self._stop_event.set()
        self._append_log("[INFO] 收到停止信号，等当前窗口结束 ...")

    def _run_worker(self, s: Settings, mode: str = "login") -> None:
        try:
            if mode == "warmup":
                from modules.warmup import run as warmup_run

                warmup_run(s, gui_queue=self._log_queue, stop_event=self._stop_event)
            else:
                run(s, gui_queue=self._log_queue, stop_event=self._stop_event,
                    base_dir=str(self._workdir))
        except Exception as exc:
            self._log_queue.put(f"[ERROR] 运行异常: {exc}")
        finally:
            self.after(0, self._on_run_finished)

    def _on_run_finished(self) -> None:
        self._set_running_state(False)
        self._refresh_records()
        try:
            self._show_finish_summary()
        except Exception:
            pass

    def _show_finish_summary(self) -> None:
        mode = getattr(self, "_task_mode", "login")
        if mode == "warmup":
            lines = [
                f"养号账号: {self._stat_vars['warm_ok'].get()}",
                f"点赞: {self._stat_vars['warm_likes'].get()}  "
                f"转推: {self._stat_vars['warm_retweets'].get()}  "
                f"关注: {self._stat_vars['warm_follows'].get()}",
            ]
        else:
            lines = [
                f"登录成功: {self._stat_vars['ok'].get()} "
                f"（token {self._stat_vars['token_ok'].get()} / "
                f"密码 {self._stat_vars['pwd_ok'].get()}）",
                f"全部失败: {self._stat_vars['fail'].get()}    "
                f"受限: {self._stat_vars['restricted'].get()}    "
                f"异常: {self._stat_vars['error'].get()}    "
                f"窗口失败: {self._stat_vars['window_error'].get()}",
            ]
        lines.append(self._progress_var.get())
        messagebox.showinfo("任务结束", "\n".join(lines))

    # ----------------------------------------------------------- 打开目录
    def _resolve_dir(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self._workdir / path

    @staticmethod
    def _open_path(path: Path) -> None:
        path = Path(path)
        if not path.exists():
            messagebox.showwarning("路径不存在", str(path))
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess

                subprocess.Popen(["open", str(path)])
            else:
                import subprocess

                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("打开失败", f"{path}\n{exc}")

    def _open_result_table(self) -> None:
        configured = self._excel_var.get().strip()
        if not configured:
            messagebox.showwarning("提示", "请先选择 token 表路径")
            return
        try:
            from modules.excel_store import resolve_result_path

            path = resolve_result_path(configured)
        except Exception:
            messagebox.showwarning("提示", "还没有可打开的结果表")
            return
        self._open_path(path)

    def _open_log_dir(self) -> None:
        self._open_path(self._resolve_dir(self._settings.output.log_dir))

    def _open_screenshot_dir(self) -> None:
        self._open_path(self._resolve_dir(self._settings.output.screenshot_dir))

    def _set_running_state(self, running: bool) -> None:
        self._is_running = running
        if running:
            self._status_dot.config(fg=COLOR_RUNNING)
            self._status_label.config(
                text="养号中 ..." if getattr(self, "_task_mode", "") == "warmup" else "运行中 ..."
            )
            self._start_btn.config(state=tk.DISABLED)
            self._stop_btn.config(state=tk.NORMAL)
            self._warmup_btn.config(state=tk.DISABLED)
            self._download_result_btn.config(state=tk.DISABLED)
            self._save_cfg_btn.config(state=tk.DISABLED)
            self._load_cfg_btn.config(state=tk.DISABLED)
            self._reset_failed_btn.config(state=tk.DISABLED)
        else:
            self._status_dot.config(fg=COLOR_IDLE)
            self._status_label.config(text="空闲")
            self._start_btn.config(state=tk.NORMAL)
            self._stop_btn.config(state=tk.DISABLED)
            self._warmup_btn.config(state=tk.NORMAL)
            self._download_result_btn.config(state=tk.NORMAL)
            self._save_cfg_btn.config(state=tk.NORMAL)
            self._load_cfg_btn.config(state=tk.NORMAL)
            self._reset_failed_btn.config(state=tk.NORMAL)

    # ----------------------------------------------------------- 日志刷新
    def _reset_log(self) -> None:
        self._log_text.config(state=tk.NORMAL); self._log_text.delete("1.0", tk.END); self._log_text.config(state=tk.DISABLED)

    def _append_log(self, text: str) -> None:
        self._log_text.config(state=tk.NORMAL)
        # 简单根据前缀着色
        tag = "INFO"
        if "[ERROR]" in text: tag = "ERROR"
        elif "[WARNING]" in text or "[WARN]" in text: tag = "WARNING"
        self._log_text.insert(tk.END, text + "\n", tag)
        self._log_text.see(tk.END)
        self._log_text.config(state=tk.DISABLED)

    def _drain_log_queue(self) -> None:
        while True:
            try:
                msg = self._log_queue.get_nowait()
            except Empty:
                break
            if msg.startswith(STATS_PREFIX):
                try:
                    self._apply_stats(json.loads(msg[len(STATS_PREFIX):]))
                except Exception:
                    pass
                continue
            self._append_log(msg)
        self.after(200, self._drain_log_queue)

    # ----------------------------------------------------------- 统计
    def _reset_stats(self) -> None:
        for v in self._stat_vars.values():
            v.set("0")
        try:
            self._progress_var.set("进度 0/0")
            self._progress_bar.config(maximum=1, value=0)
        except Exception:
            pass

    def _apply_stats(self, payload: dict) -> None:
        """根据主流程推送的结构化统计刷新右侧统计区。"""
        for key, var in self._stat_vars.items():
            if key in payload and payload[key] is not None:
                var.set(str(payload[key]))
        total = payload.get("total")
        done = payload.get("done")
        if total is None or done is None:
            return
        try:
            total_i = max(0, int(total))
            done_i = max(0, int(done))
        except (TypeError, ValueError):
            return
        self._progress_var.set(f"进度 {done_i}/{total_i}")
        try:
            self._progress_bar.config(maximum=max(1, total_i), value=min(done_i, total_i))
        except Exception:
            pass

    # ------------------------------------------------------------ 关闭
    def _on_close(self) -> None:
        if self._is_running:
            if not messagebox.askyesno("退出", "程序正在运行，确定退出？（将尝试保存并停止）"):
                return
            self._stop_event.set()
            # 等线程收尾
            if self._worker and self._worker.is_alive():
                self._worker.join(timeout=5)
        self.destroy()


def main() -> None:
    global run, Settings, load_settings
    try:
        run, Settings, load_settings = _load_core()
    except Exception:
        _report_startup_error("x-batch 启动失败", traceback.format_exc())
        return
    try:
        App().mainloop()
    except Exception:
        _report_startup_error("x-batch 运行异常", traceback.format_exc())


if __name__ == "__main__":
    main()

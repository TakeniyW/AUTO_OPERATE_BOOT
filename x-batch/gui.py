"""桌面 GUI：ixBrowser 批量推特登录 + MetaMask 钱包初始化。

启动:   python gui.py
打包exe: pip install pyinstaller && pyinstaller -F -w --name x-batch gui.py
"""
from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path
from queue import Empty
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "x-batch v1.3.2 · ixBrowser 批量推特登录 + 钱包初始化"

_WORKDIR = Path(__file__).parent.resolve()
_STARTUP_LOG = _WORKDIR / "startup_error.log"

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
        self.geometry("960x720")
        self.minsize(840, 620)

        self._workdir = Path(__file__).parent.resolve()
        self._config_path = self._workdir / "config.yaml"

        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._log_queue: queue.Queue = queue.Queue()

        self._settings = load_settings(str(self._config_path))
        self._is_running = False

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
        ttk.Button(top, text="保存配置", command=self._on_save_cfg).pack(side=tk.RIGHT, padx=(0, 8))
        ttk.Button(top, text="加载配置", command=self._on_load_cfg).pack(side=tk.RIGHT, padx=(0, 8))

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
        self._tab_records = self._build_records_tab(nb)

        nb.add(self._tab_excel, text="① Excel")
        nb.add(self._tab_ix, text="② ixBrowser")
        nb.add(self._tab_tw, text="③ Twitter")
        nb.add(self._tab_wallet, text="④ 钱包")
        nb.add(self._tab_records, text="⑤ 记录查询")

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
        ]:
            row = ttk.Frame(stats_frame)
            row.pack(fill=tk.X, pady=1)
            ttk.Label(row, text=label, width=14).pack(side=tk.LEFT)
            var = tk.StringVar(value="0")
            ttk.Label(row, textvariable=var, foreground="#2980b9", font=("", 12, "bold")).pack(side=tk.LEFT)
            self._stat_vars[key] = var

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

        self._backup_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="首次运行前备份原表 (.bak)", variable=self._backup_var).grid(
            row=7, column=0, columnspan=3, sticky="w", pady=4
        )

        ttk.Label(
            f,
            text="提示: 只跑 token 时表里只需 token 一列；启用账号密码登录后还需 账号 / 密码 / 2FA。",
            foreground="#7f8c8d",
            wraplength=380,
            justify="left",
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(10, 0))
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

        ttk.Label(f, text="提示: 脚本会先在 Python 本地生成 12 词助记词并离线派生地址，再自动点 MetaMask 完成导入。",
                  foreground="#7f8c8d", wraplength=380, justify="left").grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(10, 0)
        )
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
        return f

    # --------------------------------------------------- 设置 <-> UI
    def _load_settings_to_ui(self) -> None:
        s = self._settings
        self._excel_var.set(str(Path(s.excel.path)))
        self._token_col_var.set(s.excel.token_column)
        self._ct0_col_var.set(s.excel.ct0_column)
        self._user_col_var.set(s.excel.username_column)
        self._pwd_col_var.set(s.excel.password_column)
        self._twofa_col_var.set(s.excel.twofa_column)
        self._backup_var.set(s.excel.backup)

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
        self._wallet_pfx_var.set(s.wallet.password_prefix)
        self._wallet_plen_var.set(str(s.wallet.password_length))
        self._wallet_fallback_var.set(s.wallet.manual_fallback)
        self._on_pwd_login_toggle()

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

        s.excel.path = self._excel_var.get().strip() or "accounts.xlsx"
        s.excel.token_column = self._token_col_var.get().strip() or "auth_token"
        s.excel.ct0_column = self._ct0_col_var.get().strip()
        s.excel.username_column = self._user_col_var.get().strip() or "账号"
        s.excel.password_column = self._pwd_col_var.get().strip() or "密码"
        s.excel.twofa_column = self._twofa_col_var.get().strip() or "2FA"
        s.excel.backup = bool(self._backup_var.get())

        s.ixbrowser.host = self._ix_host_var.get().strip() or "127.0.0.1"
        s.ixbrowser.port = _int(self._ix_port_var, 53200)
        s.ixbrowser.profile_name_filter = self._ix_filter_var.get().strip()
        s.ixbrowser.profile_limit = _int(self._ix_limit_var, 1)
        s.ixbrowser.profile_start = max(1, _int(self._ix_start_var, 1))
        s.ixbrowser.profile_end = max(0, _int(self._ix_end_var, 0))
        s.ixbrowser.keep_window_open = bool(self._keep_open_var.get())

        s.twitter.login_timeout = _int(self._tw_timeout_var, 25)
        s.twitter.max_tokens_per_profile = _int(self._tw_max_var, 20)
        s.twitter.retry_per_token = _int(self._tw_retry_var, 1)
        s.twitter.delay_min = _float(self._tw_delay_min_var, 3.0)
        s.twitter.delay_max = _float(self._tw_delay_max_var, 6.0)
        s.twitter.password_login = bool(self._tw_pwd_login_var.get())
        s.twitter.password_timeout = _int(self._tw_pwd_timeout_var, 40)

        s.wallet.enabled = bool(self._wallet_enabled_var.get())
        s.wallet.mode = self._wallet_mode_var.get().strip() or "auto"
        s.wallet.extension_id = self._wallet_ext_var.get().strip()
        s.wallet.password_prefix = self._wallet_pfx_var.get().strip() or "Wx"
        s.wallet.password_length = _int(self._wallet_plen_var, 14)
        s.wallet.manual_fallback = bool(self._wallet_fallback_var.get())

        return s

    # -------------------------------------------------------------- 按钮
    def _pick_excel(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 token 表",
            filetypes=[("Excel", "*.xlsx *.xls"), ("所有文件", "*.*")],
        )
        if path:
            self._excel_var.set(path)

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
                    "token_column": s.excel.token_column,
                    "ct0_column": s.excel.ct0_column,
                    "username_column": s.excel.username_column,
                    "password_column": s.excel.password_column,
                    "twofa_column": s.excel.twofa_column,
                    "backup": s.excel.backup,
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
    def _refresh_records(self) -> None:
        try:
            from modules.state_db import StateDB

            s = self._collect_settings()
            db = StateDB(s.output.state_db)
            status = self._rec_status_var.get().strip()
            rows = db.query(
                self._rec_kw_var.get().strip(),
                "" if status == "全部" else status,
            )
        except Exception as exc:
            self._rec_summary_var.set(f"读取记录失败: {exc}")
            return

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

    # ----------------------------------------------------------- 运行控制
    def _on_start(self) -> None:
        if self._is_running:
            return
        s = self._collect_settings()

        # 基础校验
        if not Path(s.excel.path).exists():
            messagebox.showerror("参数错误", f"Excel 不存在:\n{s.excel.path}")
            return

        if not s.wallet.enabled and s.wallet.mode != "skip":
            # 让 GUI 上 radio 覆盖
            pass

        # 界面上的参数直接写回 config.yaml，避免还要手动改文件
        self._save_config(silent=True)

        self._reset_log()
        self._reset_stats()

        self._stop_event = threading.Event()
        self._log_queue = queue.Queue()
        self._is_running = True
        self._set_running_state(True)

        try:
            self._worker = threading.Thread(
                target=self._run_worker, args=(s,), daemon=True
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

    def _run_worker(self, s: Settings) -> None:
        try:
            run(s, gui_queue=self._log_queue, stop_event=self._stop_event)
        except Exception as exc:
            self._log_queue.put(f"[ERROR] 运行异常: {exc}")
        finally:
            self.after(0, self._on_run_finished)

    def _on_run_finished(self) -> None:
        self._set_running_state(False)
        self._refresh_records()

    def _set_running_state(self, running: bool) -> None:
        self._is_running = running
        if running:
            self._status_dot.config(fg=COLOR_RUNNING); self._status_label.config(text="运行中 ...")
            self._start_btn.config(state=tk.DISABLED); self._stop_btn.config(state=tk.NORMAL)
        else:
            self._status_dot.config(fg=COLOR_IDLE); self._status_label.config(text="空闲")
            self._start_btn.config(state=tk.NORMAL); self._stop_btn.config(state=tk.DISABLED)

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
        changed = False
        while True:
            try:
                msg = self._log_queue.get_nowait()
            except Empty:
                break
            self._append_log(msg)
            changed = True

        if changed:
            self._refresh_stats()
        self.after(200, self._drain_log_queue)

    # ----------------------------------------------------------- 统计
    def _reset_stats(self) -> None:
        for v in self._stat_vars.values():
            v.set("0")

    def _refresh_stats(self) -> None:
        """从日志反推统计，避免主流程与 GUI 之间再加一套回调。"""
        text = self._log_text.get("1.0", tk.END)
        stats = {k: 0 for k in self._stat_vars}
        for line in text.splitlines():
            if "token 登录成功" in line:
                stats["token_ok"] += 1
            elif "账号密码登录成功" in line:
                stats["pwd_ok"] += 1
            elif "全部登录方式失败" in line:
                stats["fail"] += 1
            elif "均不可用（受限）" in line:
                stats["restricted"] += 1
            elif "放弃当前窗口" in line:
                stats["error"] += 1
            elif "打开/连接窗口失败" in line:
                stats["window_error"] += 1
        stats["ok"] = stats["token_ok"] + stats["pwd_ok"]
        for key, value in stats.items():
            self._stat_vars[key].set(str(value))

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

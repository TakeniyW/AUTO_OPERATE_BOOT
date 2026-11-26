"""自动下载并解压 MetaMask 扩展，供 Chrome --load-extension 加载。

来源（按顺序尝试，任取其一）：
  1. Chrome 应用商店 CRX —— 始终最新，无需解析版本
  2. MetaMask GitHub Releases 的 metamask-chrome-<version>.zip

下载优先用 curl（Windows 10+/macOS/Linux 自带，兼容代理环境），
失败回退到 urllib。解压后保证 manifest.json 位于目标目录根。
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, List, Optional, Tuple

# Chrome 商店里的 MetaMask 扩展 ID（官方）
METAMASK_EXTENSION_ID = "nkbihfbeogaeaoehlefnkodbefgpgknn"

CRX_URL = (
    "https://clients2.google.com/service/update2/crx?response=redirect"
    "&prodversion=120.0&acceptformat=crx2,crx3"
    f"&x=id%3D{METAMASK_EXTENSION_ID}%26installsource%3Dondemand%26uc"
)
GITHUB_LATEST = "https://github.com/MetaMask/metamask-extension/releases/latest"

Progress = Optional[Callable[[str], None]]


def _log(msg: str, cb: Progress) -> None:
    if cb is not None:
        try:
            cb(msg)
        except Exception:
            pass
    else:
        print(msg, flush=True)


# ---------------------------------------------------------------- 下载
def _download(url: str, dest: str, cb: Progress = None, timeout: int = 180) -> bool:
    """curl 下载，失败回退 urllib。"""
    curl = shutil.which("curl")
    if curl:
        cmd = [
            curl, "-sSL", "-A", "Mozilla/5.0",
            "--connect-timeout", "20", "-m", str(timeout),
            "-o", dest, url,
        ]
        _log(f"  下载: {url}", cb)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 30)
            if r.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0:
                return True
            _log(
                f"  curl 失败 rc={r.returncode}: {(r.stderr or '').strip()[:200]}",
                cb,
            )
        except Exception as exc:
            _log(f"  curl 异常: {exc}", cb)

    _log("  回退到 urllib 下载 ...", cb)
    proxy = (
        os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")
    )
    handlers: List[urllib.request.BaseHandler] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers) if handlers else urllib.request.build_opener()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with opener.open(req, timeout=timeout) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)
        return os.path.getsize(dest) > 0
    except Exception as exc:
        _log(f"  urllib 失败: {exc}", cb)
        return False


def _github_latest_tag(cb: Progress = None) -> str:
    """从 releases/latest 的 302 Location 解析最新 tag（如 v13.58.0）。"""
    fd, hpath = tempfile.mkstemp(suffix=".h")
    os.close(fd)
    try:
        curl = shutil.which("curl")
        if curl:
            subprocess.run(
                [curl, "-sIL", "-A", "Mozilla/5.0", "-o", os.devnull, "-D", hpath, GITHUB_LATEST],
                capture_output=True, text=True, timeout=30,
            )
        try:
            text = Path(hpath).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
    finally:
        try:
            os.unlink(hpath)
        except OSError:
            pass

    for line in text.splitlines():
        m = re.search(r"/releases/tag/(v?[0-9][0-9.]*)", line, re.I)
        if m:
            return m.group(1)

    # 兜底：urllib 读 Location
    try:
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        handlers = [urllib.request.ProxyHandler({"https": proxy})] if proxy else []
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(GITHUB_LATEST, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        resp = opener.open(req, timeout=20)
        loc = resp.headers.get("Location", "")
        m = re.search(r"/releases/tag/(v?[0-9][0-9.]*)", loc, re.I)
        if m:
            return m.group(1)
    except Exception as exc:
        _log(f"  解析 GitHub 版本失败: {exc}", cb)
    return ""


# ---------------------------------------------------------------- 解压
def _crx_to_zip(data: bytes) -> bytes:
    """剥离 CRX 头（Cr24 + version + header_len），返回 zip 字节。"""
    if data[:4] == b"Cr24":
        _version, header_len = struct.unpack("<II", data[4:12])
        return data[12 + header_len:]
    return data


def _extract(zipbytes: bytes, dest_dir: str) -> Tuple[bool, str]:
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    # 清空旧内容，避免版本残留
    for child in dest.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass

    try:
        z = zipfile.ZipFile(io.BytesIO(zipbytes))
    except Exception as exc:
        return False, f"zip 解析失败: {exc}"

    names = z.namelist()
    if not any(n.endswith("manifest.json") for n in names):
        return False, "压缩包内未找到 manifest.json，可能不是扩展包"

    # 如果所有文件都在同一个顶层子目录里，则把内容提到根
    files = [n for n in names if not n.endswith("/")]
    prefix = os.path.commonprefix(files) if files else ""
    strip = prefix if prefix.endswith("/") else ""

    for info in z.infolist():
        name = info.filename
        if name.endswith("/"):
            continue
        target_name = name[len(strip):] if strip else name
        if not target_name or target_name.startswith("/"):
            continue
        out = dest / target_name
        out.parent.mkdir(parents=True, exist_ok=True)
        with z.open(info) as src, open(out, "wb") as f:
            shutil.copyfileobj(src, f)

    man = dest / "manifest.json"
    if not man.exists():
        found = list(dest.rglob("manifest.json"))
        if found:
            return False, f"manifest.json 位于子目录 {found[0].parent.name}，结构异常"
        return False, "解压后未找到 manifest.json"
    try:
        meta = json.loads(man.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"manifest.json 无效: {exc}"
    version = str(meta.get("version", ""))
    return True, version or "已安装"


# ---------------------------------------------------------------- 对外
def _resolve_name(meta: dict, dest: Path) -> str:
    """manifest 的 name 可能是 __MSG_appName__，从 _locales/en 解析真实名称。"""
    name = str(meta.get("name", ""))
    m = re.fullmatch(r"__MSG_([A-Za-z0-9_]+)__", name)
    if not m:
        return name
    key = m.group(1)
    for locale in ("en", "en_US", "en_GB"):
        msg_path = dest / "_locales" / locale / "messages.json"
        if msg_path.exists():
            try:
                messages = json.loads(msg_path.read_text(encoding="utf-8"))
                entry = messages.get(key) or messages.get(key.lower())
                if isinstance(entry, dict) and entry.get("message"):
                    return entry["message"]
            except Exception:
                pass
    return name


def extension_ready(path: str, base_dir: str = ".") -> Tuple[bool, str]:
    """检查扩展目录是否就绪。返回 (就绪, 说明)。"""
    p = Path(path)
    if not p.is_absolute():
        p = Path(base_dir) / p
    man = p / "manifest.json"
    if not man.exists():
        return False, f"未找到 {man}"
    try:
        meta = json.loads(man.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"manifest.json 读取失败: {exc}"
    version = str(meta.get("version", ""))
    name = _resolve_name(meta, p)
    if "metamask" in name.lower():
        return True, f"MetaMask {version}".strip()
    return True, (f"{name} v{version}" if name else f"v{version}").strip()


def extension_load_args(wallet_cfg, base_dir: str = ".") -> List[str]:
    """根据钱包配置生成 Chrome 启动参数（空列表表示不注入）。"""
    if not getattr(wallet_cfg, "auto_load_extension", False):
        return []
    p = Path(wallet_cfg.extension_path)
    if not p.is_absolute():
        p = Path(base_dir) / p
    ready, _ = extension_ready(str(p))
    if not ready:
        return []
    return [f"--load-extension={p.resolve()}"]


def install_metamask(
    target_dir: str = "extensions/metamask",
    base_dir: str = ".",
    cb: Progress = None,
) -> Tuple[bool, str]:
    """下载并解压 MetaMask 到 target_dir。返回 (是否成功, 说明)。"""
    dest = Path(target_dir)
    if not dest.is_absolute():
        dest = Path(base_dir) / dest
    dest = dest.resolve()
    _log(f"目标目录: {dest}", cb)

    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, "metamask.bin")
        sources: List[Tuple[str, str, bool]] = [("Chrome 应用商店", CRX_URL, True)]

        tag = _github_latest_tag(cb)
        if tag:
            version = tag.lstrip("v")
            gh_url = (
                "https://github.com/MetaMask/metamask-extension/releases/download/"
                f"{tag}/metamask-chrome-{version}.zip"
            )
            sources.append((f"GitHub {tag}", gh_url, False))
        else:
            _log("  无法解析 GitHub 最新版本，仅尝试 Chrome 应用商店", cb)

        last_err = ""
        for name, url, is_crx in sources:
            _log(f"尝试来源: {name}", cb)
            if os.path.exists(archive):
                os.unlink(archive)
            if not _download(url, archive, cb):
                last_err = f"{name} 下载失败"
                continue
            data = open(archive, "rb").read()
            if len(data) < 1024:
                last_err = f"{name} 返回内容过小（{len(data)}B）"
                continue
            zipbytes = _crx_to_zip(data) if is_crx else data
            ok, detail = _extract(zipbytes, str(dest))
            if not ok:
                last_err = f"{name}: {detail}"
                _log(f"  {last_err}", cb)
                continue
            ready, info = extension_ready(str(dest))
            if not ready:
                last_err = info
                _log(f"  校验失败: {info}", cb)
                continue
            _log(f"安装完成: {info}", cb)
            return True, info
    return False, last_err or "所有来源均失败，请检查网络或手动下载扩展"


def main(argv: List[str]) -> int:
    target = argv[1] if len(argv) > 1 else "extensions/metamask"
    ok, msg = install_metamask(target, base_dir=".")
    print("OK" if ok else f"FAILED: {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

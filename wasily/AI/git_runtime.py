"""Git 运行时分发：探测本机 Git，缺省时自动下载便携版 MinGit。

为什么需要这一层：开发机与用户机器都可能没装 Git（当前机器就没有 git.exe），
而 IDE 的 SCM、Agent 闭环都要求随时能执行 Git。这里把「找 Git」收敛成唯一入口：
  1. 环境变量 WASILY_GIT 指定
  2. 之前自动下载的托管版（%LOCALAPPDATA%\\Wasily\\git\\<版本>\\cmd\\git.exe）
  3. 系统 PATH
  4. Windows 常见安装目录
都没有时按需下载 MinGit 便携包，解压到用户目录，不写系统 PATH、不需要管理员权限。

对外能力：resolve() / ensure() / run_git()
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from typing import Any

# 固定版本便于复现；升级时只需改这里和对应校验信息
MANAGED_VERSION = os.environ.get("WASILY_GIT_VERSION") or "2.47.1"
MANIFEST_NAME = "manifest.json"
# MinGit 便携包约 50MB，留足余量但仍拦截异常大文件
MAX_ZIP_BYTES = 160 * 1024 * 1024
PROBE_TIMEOUT = 15


def runtime_dir() -> str:
    """托管 Git 的根目录：放在用户本地应用数据下，避免污染仓库与系统目录。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "Wasily", "git")


def managed_exe(version: str = "") -> str:
    return os.path.join(runtime_dir(), version or MANAGED_VERSION, "cmd", "git.exe")


def _probe(exe: str) -> str:
    """执行 git --version，返回版本号；不可用时返回空串。"""
    if not exe or not os.path.isfile(exe):
        return ""
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ""
    if r.returncode != 0:
        return ""
    m = re.search(r"git version (\S+)", (r.stdout or "") + (r.stderr or ""))
    return m.group(1) if m else ""


def _system_candidates() -> list[str]:
    out: list[str] = []
    found = shutil.which("git")
    if found:
        out.append(found)
    if os.name == "nt":
        for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.environ.get("LOCALAPPDATA")):
            if not root:
                continue
            out.append(os.path.join(root, "Git", "cmd", "git.exe"))
            out.append(os.path.join(root, "Programs", "Git", "cmd", "git.exe"))
    return [p for p in out if p and os.path.isfile(p)]


def resolve(probe: bool = True) -> dict[str, Any]:
    """按优先级解析可用 Git。probe=False 时只按文件存在判断（省一次子进程）。"""
    env_exe = str(os.environ.get("WASILY_GIT") or "").strip()
    entries = [(env_exe, "env"), (managed_exe(), "managed")]
    entries += [(p, "system") for p in _system_candidates()]
    for exe, source in entries:
        if not exe:
            continue
        if not probe:
            if os.path.isfile(exe):
                return {"ok": True, "path": exe, "version": "", "source": source}
            continue
        ver = _probe(exe)
        if ver:
            return {"ok": True, "path": exe, "version": ver, "source": source}
    return {
        "ok": False,
        "error": "未找到 Git：PATH 与常见安装目录均无 git.exe，可调用 /git/runtime/ensure 自动下载便携版",
        "managedDir": runtime_dir(),
        "managedVersion": MANAGED_VERSION,
    }


def _asset_urls(version: str) -> list[str]:
    """按 CPU 架构拼 MinGit 下载地址；优先走国内镜像，最后回落 GitHub 直连。"""
    machine = (platform.machine() or "").lower()
    if machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("x86", "i386", "i686"):
        arch = "32-bit"
    else:
        arch = "64-bit"
    rel = "git-for-windows/git/releases/download/v%s.windows.1/MinGit-%s-%s.zip" % (version, version, arch)
    return [
        "https://gh-proxy.com/https://github.com/" + rel,
        "https://ghfast.top/https://github.com/" + rel,
        "https://github.com/" + rel,
    ]


class _Fatal(RuntimeError):
    """重试其它镜像也无意义的错误（如超出体积上限）。"""


def _download(urls: list[str], dest: str, timeout: int = 120) -> tuple[int, str]:
    import requests

    last = ""
    for url in urls:
        try:
            with requests.get(url, stream=True, timeout=(15, timeout), allow_redirects=True) as r:
                if r.status_code != 200:
                    last = "%s -> HTTP %d" % (url, r.status_code)
                    continue
                total = int(r.headers.get("Content-Length") or 0)
                if total > MAX_ZIP_BYTES:
                    raise _Fatal("安装包体积异常（%d 字节），已终止下载" % total)
                digest = hashlib.sha256()
                wrote = 0
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        if not chunk:
                            continue
                        wrote += len(chunk)
                        if wrote > MAX_ZIP_BYTES:
                            raise _Fatal("安装包超过 %d 字节上限，已终止下载" % MAX_ZIP_BYTES)
                        digest.update(chunk)
                        f.write(chunk)
                if wrote == 0:
                    last = "%s -> 响应为空" % url
                    continue
                return wrote, digest.hexdigest()
        except _Fatal:
            raise
        except Exception as exc:  # noqa: BLE001  网络异常换下一个镜像
            last = "%s -> %s" % (url, exc)
    raise RuntimeError("全部下载地址均失败：" + (last or "无可用地址"))


def _safe_extract(zip_path: str, dest: str) -> None:
    """解压并拦截 zip slip（绝对路径 / ..）与符号链接，避免写出目标目录。"""
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise RuntimeError("压缩包包含非法路径：%s" % info.filename)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:  # 符号链接直接跳过，Windows 上也不需要
                continue
            zf.extract(info, dest)


def _find_git_root(root: str) -> str:
    """MinGit 一般解压后就是根目录；兼容多包一层文件夹的情况。"""
    if os.path.isfile(os.path.join(root, "cmd", "git.exe")):
        return root
    for name in os.listdir(root):
        sub = os.path.join(root, name)
        if os.path.isdir(sub) and os.path.isfile(os.path.join(sub, "cmd", "git.exe")):
            return sub
    return ""


def _write_manifest(payload: dict[str, Any]) -> None:
    path = os.path.join(runtime_dir(), MANIFEST_NAME)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def ensure(force: bool = False) -> dict[str, Any]:
    """确保存在可用 Git：已装则直接返回，否则下载 MinGit 并解压到用户目录。"""
    if not force:
        current = resolve()
        if current.get("ok"):
            current["downloaded"] = False
            return current

    version = MANAGED_VERSION
    target = os.path.join(runtime_dir(), version)
    exe = os.path.join(target, "cmd", "git.exe")
    if os.path.isfile(exe) and not force:
        ver = _probe(exe)
        if ver:
            return {"ok": True, "path": exe, "version": ver,
                    "source": "managed", "downloaded": False}

    os.makedirs(runtime_dir(), exist_ok=True)
    urls = _asset_urls(version)
    tmp_zip = os.path.join(tempfile.gettempdir(), "wasily-git-%s.zip" % uuid.uuid4().hex[:8])
    stage = os.path.join(runtime_dir(), "_stage_" + uuid.uuid4().hex[:8])
    try:
        size, digest = _download(urls, tmp_zip)
        os.makedirs(stage, exist_ok=True)
        _safe_extract(tmp_zip, stage)
        root = _find_git_root(stage)
        if not root:
            raise RuntimeError("安装包内未找到 cmd/git.exe，可能下载到了错误文件")
        # 先移除旧目录再原子改名，避免半成品目录被当成可用版本
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        os.replace(root, target)
        ver = _probe(exe)
        if not ver:
            shutil.rmtree(target, ignore_errors=True)
            raise RuntimeError("内置 Git 解压后无法执行（%s）" % exe)
        _write_manifest({
            "version": ver, "path": exe, "sha256": digest, "bytes": size,
            "installedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "urls": urls,
        })
        return {"ok": True, "path": exe, "version": ver,
                "source": "managed", "downloaded": True, "bytes": size}
    finally:
        for p in (tmp_zip,):
            try:
                os.remove(p)
            except OSError:
                pass
        if os.path.isdir(stage):
            shutil.rmtree(stage, ignore_errors=True)


def run_git(args: list[str], cwd: str | None = None, timeout: int = 120) -> dict[str, Any]:
    """给后端/Agent 的统一执行入口：缺 Git 时先自动下载再执行。"""
    current = resolve()
    if not current.get("ok"):
        current = ensure()
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")  # 避免无凭据时卡在交互输入
    try:
        r = subprocess.run([current["path"]] + list(args), cwd=cwd, capture_output=True,
                           text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "path": current["path"], "error": str(exc)}
    return {"ok": r.returncode == 0, "path": current["path"], "code": r.returncode,
            "stdout": r.stdout or "", "stderr": r.stderr or ""}


if __name__ == "__main__":
    print(json.dumps(resolve(), ensure_ascii=False, indent=2))

"""日志中心。

目录组织对齐 Trae 的习惯：默认在用户数据目录下按「启动时间戳」分目录，
    %APPDATA%\\Wasily\\logs\\20261119T135941\\api_server.log
设置页可把根目录改成任意可写路径；改完立即重建本次启动的会话目录。

只做三件事：确定目录、安全读取、交给系统打开。
"""
import os
import re
import shutil
import subprocess
import time

import config as cfg

PRODUCT = "Wasily"
_SESSION = {"path": "", "stamp": ""}
_REBIND = []
_SAFE_NAME = re.compile(r"^[^\\/:*?\"<>|]+$")


def on_root_change(fn):
    """注册日志目录切换回调，供 api_server 重新绑定文件句柄。"""
    if callable(fn) and fn not in _REBIND:
        _REBIND.append(fn)


def _notify(path):
    for fn in list(_REBIND):
        try:
            fn(path)
        except Exception:  # noqa: BLE001 —— 回调失败不能让设置保存失败
            pass


def default_root():
    """默认日志根目录：跟随产品数据目录，而不是安装目录，避免升级时被覆盖。"""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return os.path.join(appdata, PRODUCT, "logs")
    return os.path.join(os.path.expanduser("~"), ".wasily", "logs")


def configured_root():
    custom = str(cfg.get("logging.path", "") or "").strip()
    return os.path.abspath(os.path.expanduser(custom)) if custom else default_root()


def _new_session_dir(root):
    stamp = time.strftime("%Y%m%dT%H%M%S")
    path = os.path.join(root, stamp)
    os.makedirs(path, exist_ok=True)
    return path, stamp


def session_dir():
    """本次进程的日志目录（首次调用时创建）。"""
    root = configured_root()
    cur = _SESSION["path"]
    if cur and os.path.dirname(cur) == root:
        return cur
    path, stamp = _new_session_dir(root)
    _SESSION.update(path=path, stamp=stamp)
    return path


def set_root(path):
    """修改日志根目录并校验可写；返回新的会话目录。"""
    raw = str(path or "").strip()
    if not raw:
        cfg.set_value("logging.path", "")
        cfg.save()
        new_dir = _reset(configured_root())
        _notify(new_dir)
        return new_dir
    full = os.path.abspath(os.path.expanduser(raw))
    try:
        os.makedirs(full, exist_ok=True)
        probe = os.path.join(full, ".write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as exc:
        raise ValueError("目录不可写：%s" % exc)
    cfg.set_value("logging.path", full)
    cfg.save()
    new_dir = _reset(full)
    _notify(new_dir)
    return new_dir


def _reset(root):
    _SESSION.update(path="", stamp="")
    return session_dir()


def _inside(root, target):
    root = os.path.realpath(root)
    target = os.path.realpath(target)
    return target == root or target.startswith(root + os.sep)


def list_sessions(limit=30):
    root = configured_root()
    if not os.path.isdir(root):
        return []
    rows = []
    for name in os.listdir(root):
        full = os.path.join(root, name)
        if not os.path.isdir(full):
            continue
        try:
            files = [f for f in os.listdir(full)
                     if os.path.isfile(os.path.join(full, f))]
            size = sum(os.path.getsize(os.path.join(full, f)) for f in files)
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        rows.append({"name": name, "path": full, "files": len(files),
                     "size": size, "mtime": mtime})
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:limit]


def list_files(session=None, limit=100):
    """列出某个会话目录下的日志文件；缺省为本次进程会话。"""
    base = session_dir() if not session else _resolve_session(session)
    if not base:
        return []
    rows = []
    try:
        names = os.listdir(base)
    except OSError:
        return []
    for name in names:
        full = os.path.join(base, name)
        if not os.path.isfile(full):
            continue
        try:
            st = os.stat(full)
        except OSError:
            continue
        rows.append({"name": name, "path": full, "size": st.st_size,
                     "mtime": st.st_mtime})
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:limit]


def _resolve_session(session):
    if not session or not _SAFE_NAME.match(session):
        return ""
    full = os.path.join(configured_root(), session)
    return full if _inside(configured_root(), full) and os.path.isdir(full) else ""


def resolve_file(name, session=None):
    """把文件名解析为会话目录内的真实路径，拒绝目录穿越。"""
    if not name:
        name = "api_server.log"
    base = _resolve_session(session) if session else session_dir()
    if not base:
        return ""
    full = os.path.join(base, os.path.basename(name))
    return full if _inside(base, full) and os.path.isfile(full) else ""


def tail(name="api_server.log", lines=200, session=None):
    fp = resolve_file(name, session)
    if not fp:
        return ""
    lines = max(1, min(int(lines or 200), 5000))
    try:
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            data = f.readlines()
    except OSError:
        return ""
    return "".join(data[-lines:])


def info():
    root = configured_root()
    sess = session_dir()
    return {
        "root": root,
        "default_root": default_root(),
        "custom": bool(str(cfg.get("logging.path", "") or "").strip()),
        "session": os.path.basename(sess),
        "session_dir": sess,
        "files": list_files(),
        "sessions": list_sessions(20),
        "keep_days": int(cfg.get("logging.keep_days", 30) or 30),
    }


def open_folder(path=""):
    """在资源管理器中打开日志目录（仅本机 Windows 有效）。"""
    target = path or session_dir()
    root = configured_root()
    if not _inside(root, target):
        target = root
    if not os.path.isdir(target):
        target = root
        os.makedirs(target, exist_ok=True)
    if os.name == "nt":
        os.startfile(target)  # noqa: S606  —— 用户主动点击「打开日志目录」
    else:
        subprocess.Popen(["xdg-open", target])
    return target


def purge_old(days=None):
    """按保留天数清理历史会话目录，返回删除数量。"""
    keep = int(days if days is not None else cfg.get("logging.keep_days", 30) or 30)
    if keep <= 0:
        return 0
    cutoff = time.time() - keep * 86400
    removed = 0
    root = configured_root()
    if keep and os.path.isdir(root):
        for name in os.listdir(root):
            full = os.path.join(root, name)
            if full == session_dir() or not os.path.isdir(full):
                continue
            if not _SAFE_NAME.match(name):
                continue
            try:
                if os.path.getmtime(full) < cutoff:
                    shutil.rmtree(full, ignore_errors=True)
                    removed += 1
            except OSError:
                pass
    return removed

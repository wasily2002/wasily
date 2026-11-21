# -*- coding: utf-8 -*-
"""运行项目中的网页：必要时启动本地静态服务，再在 IDE 内打开。

与「改造/开发网页」严格区分：本工具只做启动与打开，不修改任何文件。
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import open_in_ide  # noqa: E402

PORT_FROM = 8123
PORT_TRIES = 20

SPEC = {
    "name": "run_web_page",
    "description": "运行（启动并打开）项目中的网页文件。用户说「运行网页/打开页面/预览/跑起来看看」时必须用这个工具，它会按需启动本地静态服务并在 IDE 内打开；不需要也不允许改动文件内容",
    "parameters": {
        "path": "网页文件的绝对路径，或相对项目根的路径，如 待办事项网页/index.html；也可直接传 http(s) 网址",
    },
}


def _port_open(port, host="127.0.0.1"):
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _serves(port, rel):
    """该端口是否已经在提供这个文件"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/%s" % (port, rel), timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _start_server(directory, port):
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", directory],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
    )


_DIRS = {}  # 目录 -> 已启动端口，避免不同目录误复用同一服务


def _ensure_server(directory, rel):
    port = _DIRS.get(directory)
    if port and _port_open(port) and _serves(port, rel):
        return port, True
    for i in range(PORT_TRIES):
        cand = PORT_FROM + i
        if _port_open(cand):
            continue  # 端口已被占用，换下一个（不猜它服务的是哪个目录）
        _start_server(directory, cand)
        for _ in range(25):
            time.sleep(0.25)
            if _port_open(cand):
                _DIRS[directory] = cand
                return cand, False
        return None, False
    return None, False


def run(path=""):
    raw = (path or "").strip().strip('"').strip("'")
    if not raw:
        return {"ok": False, "opened": False, "mode": "none", "error": "缺少 path（网页文件路径或网址）"}

    # 已经是网址：直接打开
    if raw.lower().startswith(("http://", "https://")):
        r = dict(open_in_ide.run(raw) or {})
        r.setdefault("mode", "ide")
        return r

    target = os.path.abspath(raw)
    if not os.path.isfile(target):
        return {"ok": False, "opened": False, "mode": "none",
                "error": "文件不存在：%s" % target}
    if not target.lower().endswith((".html", ".htm")):
        return {"ok": False, "opened": False, "mode": "none",
                "error": "不是网页文件（需要 .html/.htm）：%s" % target}

    directory = os.path.dirname(target)
    rel = urllib.request.quote(os.path.basename(target))

    port, reused = _ensure_server(directory, rel)
    if not port:
        return {"ok": False, "opened": False, "mode": "none",
                "error": "本地静态服务启动失败（端口 %d-%d 均不可用）" % (PORT_FROM, PORT_FROM + PORT_TRIES)}

    url = "http://127.0.0.1:%d/%s" % (port, rel)
    r = dict(open_in_ide.run(url, os.path.basename(target)) or {})
    r.update({
        "file": target,
        "url": url,
        "port": port,
        "server_reused": reused,
        "message": ("已启动本地服务并%s打开：%s" % ("在 IDE 内" if r.get("mode") == "ide" else "在浏览器", url)),
    })
    r.setdefault("ok", bool(r.get("opened")))
    return r

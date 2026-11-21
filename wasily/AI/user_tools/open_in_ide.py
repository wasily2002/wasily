# -*- coding: utf-8 -*-
"""在 IDE 内部标签页打开网页；不允许被嵌入的站点自动回退到系统浏览器。"""

import json
import os
import sys
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import _homepage_probe  # noqa: E402

BRIDGE = "http://127.0.0.1:18753"

SPEC = {
    "name": "open_in_ide",
    "description": "在 IDE 内部的标签页打开网页（本地/自建页面效果最好）。若目标站点禁止被嵌入或 IDE 不在线，会自动改用系统浏览器打开；返回 mode=ide 表示真的在 IDE 里开了，mode=browser 表示回退到了外部浏览器",
    "parameters": {
        "url": "要打开的网址（可省略协议，本机地址自动用 http）",
        "title": "可选，标签页标题",
    },
}


def _normalize(u):
    u = (u or "").strip()
    if not u:
        return ""
    if "://" not in u:
        host = u.split("/")[0].lower()
        local = host.startswith(("127.0.0.1", "localhost", "0.0.0.0"))
        u = ("http://" if local else "https://") + u
    return u


def _blocks_embedding(url, timeout=6):
    """响应头是否禁止被 iframe 嵌入（X-Frame-Options / CSP frame-ancestors）"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 wasily-ide"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            head = {k.lower(): v for k, v in r.headers.items()}
    except Exception:
        return False  # 探测失败不阻塞，交给内嵌尝试
    xfo = (head.get("x-frame-options") or "").lower()
    if "deny" in xfo or "sameorigin" in xfo:
        return True
    csp = (head.get("content-security-policy") or "").lower()
    if "frame-ancestors" in csp:
        seg = csp.split("frame-ancestors", 1)[1].split(";", 1)[0]
        if "'none'" in seg or "'self'" in seg:
            return True
    return False


def _bridge_alive():
    try:
        with urllib.request.urlopen(BRIDGE + "/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _open_in_ide(url, title):
    q = urllib.parse.urlencode({"url": url, "title": title or url})
    with urllib.request.urlopen(BRIDGE + "/open_url?" + q, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def run(url="", title=""):
    target = _normalize(url)
    if not target:
        return {"ok": False, "opened": False, "mode": "none", "error": "缺少 url"}

    local = target.startswith(("http://127.0.0.1", "http://localhost", "http://0.0.0.0"))
    reason = ""
    if not local and _blocks_embedding(target):
        reason = "目标站点禁止被嵌入（X-Frame-Options / CSP）"
    elif not _bridge_alive():
        reason = "IDE 桥 18753 未就绪"

    if not reason:
        try:
            r = _open_in_ide(target, title)
            if r.get("ok"):
                return {"ok": True, "opened": True, "mode": "ide", "url": target,
                        "message": "已在 IDE 内的标签页打开"}
            reason = "IDE 面板拒绝打开: %s" % r.get("error", "")
        except Exception as e:
            reason = "IDE 桥调用失败: %s" % e

    res = dict(_homepage_probe.open_page(target) or {})
    ok = bool(res.get("opened"))
    res.update({
        "ok": ok,
        "mode": "browser",
        "url": target,
        "fallback_reason": reason,
        "message": "已改用系统浏览器打开" if ok else "打开失败",
    })
    return res

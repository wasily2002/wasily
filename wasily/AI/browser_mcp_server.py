# -*- coding: utf-8 -*-
"""内置浏览器 MCP 服务（CDP 驱动）。

协议层与驱动层分离：上层是标准 MCP 工具，下层是 Chrome DevTools Protocol。
后续替换成其他浏览器内核时，只需替换 driver 的 _cmd/_new_tab 实现，
工具定义与第三方调用方式不变。
"""
import base64
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request

import config as cfg

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SHOT_DIR = os.path.join(BASE_DIR, "outputs", "browser")
PROTOCOL = "2024-11-05"
SERVER = {"name": "wasily-browser", "version": "1.0.0"}

_STATE = {"ws": None, "target": "", "url": "", "seq": 0}


def _port():
    try:
        return int(cfg.get("browser.cdp_port", 9222) or 9222)
    except Exception:  # noqa: BLE001
        return 9222


def _http(path, method="GET", timeout=5):
    url = "http://127.0.0.1:%d%s" % (_port(), path)
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "ignore")
    return json.loads(raw) if raw.strip() else {}


def _exe():
    custom = str(cfg.get("browser.executable", "") or "").strip()
    if custom and os.path.isfile(custom):
        return custom
    candidates = [
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""),
                     r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""),
                     r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""),
                     r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     r"Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return ""


def _alive():
    try:
        _http("/json/version", timeout=2)
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_browser():
    if _alive():
        return True
    if not bool(cfg.get("browser.auto_launch", True)):
        return False
    exe = _exe()
    if not exe:
        return False
    profile = os.path.join(os.environ.get("TEMP", BASE_DIR), "wasily-cdp-profile")
    subprocess.Popen([exe, "--remote-debugging-port=%d" % _port(),
                      "--user-data-dir=" + profile, "--no-first-run",
                      "--no-default-browser-check", "about:blank"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        time.sleep(0.5)
        if _alive():
            return True
    return False


def _new_tab(url):
    target = urllib.parse.quote(url, safe=":/?&=%#")
    try:
        data = _http("/json/new?" + target, method="PUT", timeout=10)
    except Exception:  # noqa: BLE001 —— 旧版浏览器接受 GET
        data = _http("/json/new?" + target, timeout=10)
    return data


def _connect(ws_url):
    import websocket
    if _STATE["ws"]:
        try:
            _STATE["ws"].close()
        except Exception:  # noqa: BLE001
            pass
    ws = websocket.create_connection(ws_url, timeout=15)
    _STATE["ws"] = ws
    return ws


def _cmd(method, params=None, timeout=20):
    import websocket
    ws = _STATE["ws"]
    if ws is None:
        raise RuntimeError("浏览器未连接，请先调用 browser_open")
    _STATE["seq"] += 1
    mid = _STATE["seq"]
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msg = json.loads(ws.recv())
        except websocket.WebSocketTimeoutException:
            break
        if msg.get("id") == mid:
            if "error" in msg:
                raise RuntimeError(msg["error"].get("message") or "CDP 调用失败")
            return msg.get("result") or {}
    raise TimeoutError("CDP 响应超时：%s" % method)


def _eval(expr):
    res = _cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                    "awaitPromise": True})
    return (res.get("result") or {}).get("value")


def _ensure_page():
    if not ensure_browser():
        raise RuntimeError("未检测到可调试的浏览器；请安装 Edge/Chrome，"
                           "或把 browser.executable 指向浏览器可执行文件")
    if _STATE["ws"] is None:
        tabs = _http("/json/list")
        page = next((t for t in tabs if t.get("type") == "page"), None)
        if page is None:
            page = _new_tab("about:blank")
        _STATE["target"] = page.get("id") or ""
        _STATE["url"] = page.get("url") or ""
        _connect(page["webSocketDebuggerUrl"])


def open_url(url):
    if not url:
        raise ValueError("缺少 url")
    if not ensure_browser():
        raise RuntimeError("无法启动浏览器")
    page = _new_tab(url)
    _STATE["target"] = page.get("id") or ""
    _STATE["url"] = url
    _connect(page["webSocketDebuggerUrl"])
    time.sleep(0.6)
    return {"ok": True, "url": url, "target": _STATE["target"],
            "title": _eval("document.title") or ""}


def navigate(url):
    _ensure_page()
    _cmd("Page.navigate", {"url": url})
    time.sleep(0.8)
    _STATE["url"] = url
    return {"ok": True, "url": _eval("location.href") or url,
            "title": _eval("document.title") or ""}


def snapshot(max_chars=8000):
    _ensure_page()
    text = _eval("document.body ? document.body.innerText : ''") or ""
    text = str(text)
    return {"ok": True, "url": _eval("location.href") or _STATE["url"],
            "title": _eval("document.title") or "",
            "truncated": len(text) > max_chars, "text": text[:max_chars]}


def click(selector="", text=""):
    _ensure_page()
    if selector:
        js = ("(function(){var e=document.querySelector(%s);"
              "if(!e)return 'NOT_FOUND';e.click();return 'OK';})()"
              % json.dumps(selector))
    elif text:
        js = ("(function(){var t=%s;var els=[].slice.call("
              "document.querySelectorAll('a,button,input,[role=button]'));"
              "var e=els.find(function(x){return (x.innerText||x.value||'').trim().includes(t)});"
              "if(!e)return 'NOT_FOUND';e.click();return 'OK';})()" % json.dumps(text))
    else:
        raise ValueError("需要 selector 或 text")
    res = _eval(js)
    if res == "NOT_FOUND":
        raise RuntimeError("未找到目标元素")
    time.sleep(0.5)
    return {"ok": True, "clicked": selector or text,
            "url": _eval("location.href") or ""}


def fill(selector, value):
    _ensure_page()
    js = ("(function(){var e=document.querySelector(%s);if(!e)return 'NOT_FOUND';"
          "var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
          "s.call(e,%s);e.dispatchEvent(new Event('input',{bubbles:true}));"
          "e.dispatchEvent(new Event('change',{bubbles:true}));return 'OK';})()"
          % (json.dumps(selector), json.dumps(str(value))))
    if _eval(js) == "NOT_FOUND":
        raise RuntimeError("未找到输入框：%s" % selector)
    return {"ok": True, "selector": selector, "length": len(str(value))}


def screenshot(path=""):
    _ensure_page()
    res = _cmd("Page.captureScreenshot", {"format": "png"})
    os.makedirs(SHOT_DIR, exist_ok=True)
    out = path or os.path.join(SHOT_DIR, "shot-%s.png" % time.strftime("%Y%m%d-%H%M%S"))
    with open(out, "wb") as f:
        f.write(base64.b64decode(res.get("data") or ""))
    return {"ok": True, "path": out, "bytes": os.path.getsize(out)}


def status():
    alive = _alive()
    exe = _exe()
    return {"ok": True, "alive": alive, "driver": "cdp", "port": _port(),
            "executable": exe, "auto_launch": bool(cfg.get("browser.auto_launch", True)),
            "connected": _STATE["ws"] is not None, "target": _STATE["target"],
            "url": _STATE["url"]}


def _schema(props, required=None):
    return {"type": "object", "properties": props, "required": required or []}


TOOLS = [
    {"name": "browser_status",
     "description": "查看内置浏览器驱动状态：端口、可执行文件、连接情况。",
     "inputSchema": _schema({})},
    {"name": "browser_open",
     "description": "打开新标签页并访问 URL，返回标题与当前地址。",
     "inputSchema": _schema({"url": {"type": "string"}}, ["url"])},
    {"name": "browser_navigate",
     "description": "在当前标签页跳转到新地址。",
     "inputSchema": _schema({"url": {"type": "string"}}, ["url"])},
    {"name": "browser_snapshot",
     "description": "读取当前页面可见文本，供理解与总结。",
     "inputSchema": _schema({"max_chars": {"type": "integer"}})},
    {"name": "browser_click",
     "description": "点击元素：可用 CSS selector 或可见文字。",
     "inputSchema": _schema({"selector": {"type": "string"},
                             "text": {"type": "string"}})},
    {"name": "browser_fill",
     "description": "向输入框写入内容并触发 input/change 事件。",
     "inputSchema": _schema({"selector": {"type": "string"},
                             "value": {"type": "string"}},
                            ["selector", "value"])},
    {"name": "browser_screenshot",
     "description": "截取当前页面，保存到 outputs/browser。",
     "inputSchema": _schema({"path": {"type": "string"}})},
    {"name": "browser_eval",
     "description": "在页面执行 JavaScript 并返回结果（受 browser.allow_eval 控制）。",
     "inputSchema": _schema({"script": {"type": "string"}}, ["script"])},
    {"name": "browser_close",
     "description": "关闭当前调试连接（不强制关闭用户浏览器窗口）。",
     "inputSchema": _schema({})},
]


def _call(name, args):
    if name == "browser_status":
        return status()
    if name == "browser_open":
        return open_url(str(args.get("url") or ""))
    if name == "browser_navigate":
        return navigate(str(args.get("url") or ""))
    if name == "browser_snapshot":
        return snapshot(int(args.get("max_chars") or 8000))
    if name == "browser_click":
        return click(selector=str(args.get("selector") or ""),
                     text=str(args.get("text") or ""))
    if name == "browser_fill":
        return fill(str(args.get("selector") or ""), args.get("value") or "")
    if name == "browser_screenshot":
        return screenshot(str(args.get("path") or ""))
    if name == "browser_eval":
        if not bool(cfg.get("browser.allow_eval", True)):
            raise RuntimeError("browser.allow_eval 已关闭")
        _ensure_page()
        return {"ok": True, "result": _eval(str(args.get("script") or ""))}
    if name == "browser_close":
        if _STATE["ws"]:
            try:
                _STATE["ws"].close()
            except Exception:  # noqa: BLE001
                pass
        _STATE.update(ws=None, target="", url="")
        return {"ok": True, "message": "已断开调试连接"}
    raise KeyError("未知工具：%s" % name)


def _send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req):
    mid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    if method == "initialize":
        return _send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER}})
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _send({"jsonrpc": "2.0", "id": mid, "result": {}})
    if method == "tools/list":
        return _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    if method == "tools/call":
        name = params.get("name")
        try:
            out = _call(name, params.get("arguments") or {})
            _send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(out, ensure_ascii=False, indent=2)}],
                "isError": False}})
        except Exception as exc:  # noqa: BLE001
            _send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps({
                    "ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                    "trace": traceback.format_exc(limit=2)}, ensure_ascii=False)}],
                "isError": True}})
        return None
    return _send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": "不支持的方法：%s" % method}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception as exc:  # noqa: BLE001
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "解析失败：%s" % exc}})


if __name__ == "__main__":
    main()

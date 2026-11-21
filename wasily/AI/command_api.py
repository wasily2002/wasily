"""HTTP 路由：/v1/commands/*（跨页命令同步）。

只做参数解析、本地访问校验与 SSE 写出；所有状态在 command_bus 里，
页面拿到的永远是同一份事实，不存在两个页面各自维护副本的问题。
"""
from __future__ import annotations

import json
import os
import queue
from typing import Any
from urllib.parse import parse_qs

import command_bus

_DOCK_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "command_dock.js")

# SSE 单连接的保活与寿命：15s 一次心跳，30 分钟主动断开让浏览器重连
_HEARTBEAT_SEC = 15
_MAX_IDLE = 120


def _q(query: str) -> dict[str, str]:
    return {k: v[-1] for k, v in parse_qs(query or "", keep_blank_values=True).items()}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def handles_get(path: str) -> bool:
    return path.startswith("/v1/commands")


def handles_post(path: str) -> bool:
    return path.startswith("/v1/commands")


def _is_local(handler: Any) -> bool:
    try:
        addr = (handler.client_address or [""])[0]
    except Exception:  # noqa: BLE001
        return False
    return addr in ("127.0.0.1", "::1", "localhost")


def _send(handler: Any, payload: dict, status: int = 200) -> bool:
    handler._send_json(status, payload)  # noqa: SLF001
    return True


def _write_event(handler: Any, ev: dict) -> None:
    payload = "id: %d\ndata: %s\n\n" % (
        ev["id"], json.dumps(ev, ensure_ascii=False))
    handler.wfile.write(payload.encode("utf-8"))
    handler.wfile.flush()


def _serve_stream(handler: Any, after: int, run_id: str) -> bool:
    """SSE：先重放 after 之后的事件，再持续推送新事件。

    重放与订阅之间存在竞态，因此先订阅再重放，并按 last 序号去重，
    保证既不丢事件也不重复下发。
    """
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "close")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    handler.close_connection = True

    token, q = command_bus.subscribe()
    last = int(after or 0)
    idle = 0
    try:
        for ev in command_bus.events_after(last, run_id):
            if ev["id"] <= last:
                continue
            _write_event(handler, ev)
            last = ev["id"]
        while idle < _MAX_IDLE:
            try:
                ev = q.get(timeout=_HEARTBEAT_SEC)
            except queue.Empty:
                handler.wfile.write(b": ping\n\n")
                handler.wfile.flush()
                idle += 1
                continue
            idle = 0
            if ev["id"] <= last:
                continue          # 重放阶段已经发过
            if run_id and ev.get("run_id") != run_id:
                last = ev["id"]
                continue
            _write_event(handler, ev)
            last = ev["id"]
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass                      # 页面关闭 / 刷新
    finally:
        command_bus.unsubscribe(token)
    return True


def handle_get(handler: Any, path: str, query: str = "") -> bool:
    if not handles_get(path):
        return False
    q = _q(query)
    if path == "/v1/commands/stream":
        # 浏览器自动重连会带 Last-Event-ID；优先用它，避免退回初次 after 导致重复重放
        after = _int(handler.headers.get("Last-Event-ID"), 0)
        if not after:
            after = _int(q.get("after"), 0)
        return _serve_stream(handler, after, str(q.get("run") or ""))
    if path == "/v1/commands/dock.js":
        try:
            body = open(_DOCK_JS, "r", encoding="utf-8").read()  # noqa: SIM115
        except OSError:
            handler._set_headers(404)  # noqa: SLF001
            handler.wfile.write(b"command_dock.js not found")
            return True
        handler._set_headers(200, "application/javascript; charset=utf-8")  # noqa: SLF001
        handler.wfile.write(body.encode("utf-8"))
        return True
    if path == "/v1/commands/detail":
        run_id = str(q.get("id") or q.get("run_id") or "")
        try:
            return _send(handler, command_bus.detail(run_id, _int(q.get("after"), 0)))
        except KeyError as exc:
            return _send(handler, {"ok": False, "error": str(exc)}, 404)
    if path == "/v1/commands/stats":
        return _send(handler, {"ok": True, **command_bus.stats()})
    if path == "/v1/commands":
        return _send(handler, {"ok": True, "commands": command_bus.list_runs(_int(q.get("limit"), 30)),
                               "stats": command_bus.stats()})
    return False


def handle_post(handler: Any, path: str, data: dict) -> bool:
    if not handles_post(path):
        return False
    data = data if isinstance(data, dict) else {}
    # 命令执行是能落地的副作用：只允许本机页面调用，避免 0.0.0.0 监听被远端利用
    if path in ("/v1/commands/start", "/v1/commands/abort", "/v1/commands/ingest") \
            and not _is_local(handler):
        return _send(handler, {"ok": False, "error": "仅允许本机调用"}, 403)
    if path == "/v1/commands/start":
        try:
            return _send(handler, {"ok": True, "run": command_bus.start(
                str(data.get("command") or ""), str(data.get("cwd") or ""),
                str(data.get("source") or "ide"), str(data.get("session_id") or ""),
                _int(data.get("timeout"), 0), str(data.get("owner") or ""),
                data.get("meta") if isinstance(data.get("meta"), dict) else None)})
        except ValueError as exc:
            return _send(handler, {"ok": False, "error": str(exc)}, 400)
    if path == "/v1/commands/abort":
        try:
            return _send(handler, command_bus.abort(
                str(data.get("id") or data.get("run_id") or ""),
                str(data.get("by") or ""), str(data.get("reason") or "aborted")))
        except KeyError as exc:
            return _send(handler, {"ok": False, "error": str(exc)}, 404)
    if path == "/v1/commands/ingest":
        try:
            return _send(handler, command_bus.ingest(data))
        except KeyError as exc:
            return _send(handler, {"ok": False, "error": str(exc)}, 404)
    return False


def inject_dock(html: str) -> str:
    """把同步面板内联进页面。

    为什么不用外链：IDE webview 的 CSP 只允许 script-src 'unsafe-inline'，
    外链脚本会被直接拦掉；内联是唯一能同时覆盖两个页面的方式。
    代码本身仍是独立文件 command_dock.js，只在这里读取一次。
    """
    try:
        src = open(_DOCK_JS, "r", encoding="utf-8").read()  # noqa: SIM115
    except OSError:
        return html
    if "window.__WASILY_CMD_DOCK__" in html:
        return html
    block = ('<script>window.__WASILY_CMD_DOCK__=1;'
             'window.__WASILY_CMD_BASE__=(function(){var b="__SERVER__";'
             'return b.indexOf("__")===0?location.origin:b;})();</script>'
             '<script>' + src + '</script>')
    if "</body>" in html:
        return html.replace("</body>", block + "</body>", 1)
    return html + block

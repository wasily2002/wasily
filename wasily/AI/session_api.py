"""HTTP 路由：/v1/sync/*（跨端对话与执行状态同步）。

只负责参数解析、本机校验与 SSE 写出；状态与顺序全部在 session_bus，
两端拿到的永远是同一份事实，不需要任何一端再维护副本。
"""
from __future__ import annotations

import json
import os
import queue
from typing import Any
from urllib.parse import parse_qs

import session_bus

_DOCK_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_dock.js")

_HEARTBEAT_SEC = 12          # SSE 心跳间隔
_MAX_IDLE = 180              # 空闲连接主动断开，让浏览器重连并重新对齐


def _q(query: str) -> dict[str, str]:
    return {k: v[-1] for k, v in parse_qs(query or "", keep_blank_values=True).items()}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def handles_get(path: str) -> bool:
    return path.startswith("/v1/sync")


def handles_post(path: str) -> bool:
    return path.startswith("/v1/sync")


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
    handler.wfile.write(("id: %d\ndata: %s\n\n" % (
        int(ev.get("id") or 0), json.dumps(ev, ensure_ascii=False))).encode("utf-8"))
    handler.wfile.flush()


def _serve_stream(handler: Any, after: int, session_id: str, group: str) -> bool:
    """先重放 after 之后的事件，再持续推送；按会话与分组过滤。

    订阅先于重放，并用 last 序号去重：既不丢事件，也不重复下发。
    若客户端序号已被环形缓冲淘汰，先发 reset 让它走快照全量对齐。
    """
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "close")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    handler.close_connection = True

    token, q = session_bus.subscribe()
    last = int(after or 0)
    idle = 0
    try:
        oldest = session_bus.oldest_id()
        if last and oldest and last < oldest - 1:
            gap = {"id": session_bus.stats()["seq"] + 0, "type": "reset",
                   "ts": 0, "session_id": session_id, "source": "server",
                   "reason": "gap", "oldest": oldest}
            # reset 不占用真实序号空间：直接用最新序号，客户端收到后按快照重建
            gap["id"] = session_bus.stats()["seq"]
            _write_event(handler, gap)
            last = session_bus.stats()["seq"]
        for ev in session_bus.events_after(last):
            if _skip(ev, session_id, group):
                last = int(ev["id"])
                continue
            _write_event(handler, ev)
            last = int(ev["id"])
        while idle < _MAX_IDLE:
            try:
                ev = q.get(timeout=_HEARTBEAT_SEC)
            except queue.Empty:
                handler.wfile.write(b": ping\n\n")
                handler.wfile.flush()
                idle += 1
                continue
            idle = 0
            if int(ev["id"]) <= last:
                continue                      # 重放阶段已经发过
            if _skip(ev, session_id, group):
                last = int(ev["id"])
                continue
            _write_event(handler, ev)
            last = int(ev["id"])
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass                                  # 页面关闭 / 刷新
    finally:
        session_bus.unsubscribe(token)
    return True


def _skip(ev: dict, session_id: str, group: str) -> bool:
    """会话过滤要允许「跟随对端」：session_id 为空时只按分组收。"""
    if group and str(ev.get("group") or "default") != group:
        return True
    if session_id and str(ev.get("session_id") or "") != session_id:
        return True
    return False


def handle_get(handler: Any, path: str, query: str = "") -> bool:
    if not handles_get(path):
        return False
    q = _q(query)
    if path == "/v1/sync/stream":
        # 浏览器自动重连带 Last-Event-ID；优先用它，避免退回初次 after 重复重放
        after = _int(handler.headers.get("Last-Event-ID"), 0)
        if not after:
            after = _int(q.get("after"), 0)
        sid = str(q.get("session_id") or q.get("sid") or "")
        return _serve_stream(handler, after, sid, str(q.get("group") or ""))
    if path == "/v1/sync/dock.js":
        try:
            body = open(_DOCK_JS, "r", encoding="utf-8").read()  # noqa: SIM115
        except OSError:
            handler._set_headers(404)  # noqa: SLF001
            handler.wfile.write(b"sync_dock.js not found")
            return True
        handler._set_headers(200, "application/javascript; charset=utf-8")  # noqa: SLF001
        handler.wfile.write(body.encode("utf-8"))
        return True
    if path == "/v1/sync/snapshot":
        return _send(handler, session_bus.snapshot(str(q.get("session_id") or q.get("sid") or "")))
    if path == "/v1/sync/state":
        # 轮询降级通道：SSE 不可用时前端按 after 拉增量
        after = _int(q.get("after"), 0)
        sid = str(q.get("session_id") or q.get("sid") or "")
        group = str(q.get("group") or "")
        evs = [e for e in session_bus.events_after(after) if not _skip(e, sid, group)]
        return _send(handler, {"ok": True, "events": evs, "seq": session_bus.stats()["seq"],
                               "stats": session_bus.stats()})
    if path == "/v1/sync/verify":
        return _send(handler, session_bus.verify(
            str(q.get("session_id") or q.get("sid") or ""),
            _int(q.get("rev"), -1),
            str(q.get("hash") or ""),
            _int(q.get("count"), -1),
            _int(q.get("seq"), -1)))
    if path == "/v1/sync/stats":
        return _send(handler, {"ok": True, **session_bus.stats()})
    return False


_REPORT_KINDS = ("code_insert", "code_gen", "debug", "terminal", "command", "file_change")


def handle_post(handler: Any, path: str, data: dict) -> bool:
    if not handles_post(path):
        return False
    data = data if isinstance(data, dict) else {}
    # 上报会进入共享事件流：只允许本机，避免远端伪造对话或执行状态
    if not _is_local(handler):
        return _send(handler, {"ok": False, "error": "仅允许本机调用"}, 403)
    if path == "/v1/sync/report":
        kind = str(data.get("kind") or "command")
        if kind not in _REPORT_KINDS:
            kind = "command"
        sid = str(data.get("session_id") or data.get("sid") or "")
        ev = session_bus.publish(
            "report", session_id=sid, source=str(data.get("source") or "ide"),
            group=str(data.get("group") or "default"), kind=kind,
            title=str(data.get("title") or ""), detail=str(data.get("detail") or ""),
            path=str(data.get("path") or ""), command=str(data.get("command") or ""),
            status=str(data.get("status") or ""), output=str(data.get("output") or "")[:2000],
            run_id=str(data.get("run_id") or ""))
        return _send(handler, {"ok": True, "event": ev})
    if path == "/v1/sync/verify":
        return _send(handler, session_bus.verify(
            str(data.get("session_id") or data.get("sid") or ""),
            _int(data.get("rev"), -1), str(data.get("hash") or ""),
            _int(data.get("count"), -1), _int(data.get("seq"), -1)))
    if path == "/v1/sync/snapshot":
        return _send(handler, session_bus.snapshot(str(data.get("session_id") or data.get("sid") or "")))
    return False


def inject_dock(html: str) -> str:
    """把两端共用的同步层内联进页面。

    为什么不用外链：IDE webview 的 CSP 只允许 script-src 'unsafe-inline'，
    外链脚本会被拦掉；内联是同时覆盖 Agent 与 IDE 两页的唯一方式。
    """
    try:
        src = open(_DOCK_JS, "r", encoding="utf-8").read()  # noqa: SIM115
    except OSError:
        return html
    if "window.__WASILY_SYNC_DOCK__" in html:
        return html
    block = ('<script>window.__WASILY_SYNC_DOCK__=1;'
             'window.__WASILY_SYNC_BASE__=(function(){var b="__SERVER__";'
             'return b.indexOf("__")===0?location.origin:b;})();</script>'
             '<script>' + src + '</script>')
    if "</body>" in html:
        return html.replace("</body>", block + "</body>", 1)
    return html + block

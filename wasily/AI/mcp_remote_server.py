# -*- coding: utf-8 -*-
"""最小可用的远程 MCP Server（Streamable HTTP，JSON 响应）。

用途：给 wasily/AI/mcp.json 里的 `remote` 条目提供真实可连的服务，
      验证 HTTP 传输、initialize 握手、tools/list 与 tools/call 全链路。

启动：python -X utf8 mcp_remote_server.py [--port 9000] [--host 127.0.0.1]
接口：POST /mcp   GET /health
"""

import argparse
import json
import os
import platform
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "wasily-remote-mcp"
SERVER_VERSION = "1.0.0"

TOOLS = [
    {
        "name": "remote_echo",
        "description": "回显文本，并附带处理该请求的远程进程标识，用于确认请求确实到达远程 server",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文本"}},
            "required": ["text"],
        },
    },
    {
        "name": "remote_time",
        "description": "返回远程服务所在机器的当前时间（ISO 8601）与时区偏移",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "remote_info",
        "description": "返回远程服务的运行环境信息：主机名、操作系统、Python 版本、进程 PID 与运行时长",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_STARTED_AT = time.time()


def _call_tool(name, args):
    if name == "remote_echo":
        text = str(args.get("text", ""))
        return "remote-echo[%s]: %s" % (SERVER_NAME, text)
    if name == "remote_time":
        now = time.time()
        local = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
        return "%s (tz offset %+d min)" % (local, -time.timezone // 60)
    if name == "remote_info":
        return json.dumps({
            "server": SERVER_NAME,
            "version": SERVER_VERSION,
            "host": platform.node(),
            "os": "%s %s" % (platform.system(), platform.release()),
            "python": platform.python_version(),
            "pid": os.getpid(),
            "uptime_sec": round(time.time() - _STARTED_AT, 1),
        }, ensure_ascii=False)
    raise KeyError(name)


def _rpc_result(req):
    method = req.get("method")
    params = req.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            text = _call_tool(name, args)
        except KeyError:
            return {"content": [{"type": "text", "text": "未知工具: %s" % name}], "isError": True}
        except Exception as exc:  # noqa: BLE001
            return {"content": [{"type": "text", "text": "调用失败: %s" % exc}], "isError": True}
        return {"content": [{"type": "text", "text": text}], "isError": False}
    raise KeyError(method or "")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "wasily-remote-mcp/1.0"

    def log_message(self, fmt, *args):  # 只打印关键行，避免刷屏
        sys.stdout.write("[remote-mcp] " + (fmt % args) + "\n")
        sys.stdout.flush()

    def _send(self, code, payload=None, extra_headers=None):
        body = b""
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Mcp-Session-Id")
        if body:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send(200, {"ok": True, "server": SERVER_NAME, "tools": len(TOOLS)})
        else:
            self._send(405, {"error": "MCP 端点仅接受 POST"})

    def do_POST(self):
        if not self.path.startswith("/mcp"):
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            msg = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            self._send(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "parse error: %s" % exc}})
            return

        batch = isinstance(msg, list)
        items = msg if batch else [msg]
        out = []
        for req in items:
            if not isinstance(req, dict):
                continue
            mid = req.get("id")
            method = req.get("method")
            # 通知（无 id）不需要响应
            if mid is None:
                continue
            try:
                result = _rpc_result(req)
                out.append({"jsonrpc": "2.0", "id": mid, "result": result})
            except KeyError:
                out.append({"jsonrpc": "2.0", "id": mid,
                            "error": {"code": -32601, "message": "method not found: %s" % method}})
            except Exception as exc:  # noqa: BLE001
                out.append({"jsonrpc": "2.0", "id": mid,
                            "error": {"code": -32603, "message": str(exc)}})

        sid = self.headers.get("Mcp-Session-Id") or uuid.uuid4().hex
        if not out:
            self._send(202, None, {"Mcp-Session-Id": sid})
            return
        self._send(200, out if batch else out[0], {"Mcp-Session-Id": sid})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("[remote-mcp] listening on http://%s:%d/mcp (tools=%d)" % (args.host, args.port, len(TOOLS)))
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

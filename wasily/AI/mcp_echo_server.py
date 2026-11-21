# -*- coding: utf-8 -*-
"""最小可用的本地 MCP Server（stdio 传输，真 JSON-RPC 2.0）

用途：验证 wasily 的 MCP 中枢「能连 + 能列工具 + 能调工具」的真实链路。
方法：initialize / tools/list / tools/call / ping；工具：echo_text、add_numbers。
"""
import json
import sys

PROTOCOL = "2024-11-05"

TOOLS = [
    {
        "name": "echo_text",
        "description": "原样返回传入的文本，并在末尾附加字符数（用于连通性验证）",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文本"}},
            "required": ["text"],
        },
    },
    {
        "name": "add_numbers",
        "description": "把两个整数相加，返回和",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    },
]


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def result(id_, res):
    send({"jsonrpc": "2.0", "id": id_, "result": res})


def error(id_, code, msg):
    send({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": msg}})


def handle(req):
    mid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    if method == "initialize":
        result(mid, {"protocolVersion": PROTOCOL,
                     "capabilities": {"tools": {"listChanged": False}},
                     "serverInfo": {"name": "wasily-echo", "version": "1.0.0"}})
    elif method in ("notifications/initialized", "notifications/cancelled"):
        pass
    elif method == "ping":
        result(mid, {})
    elif method == "tools/list":
        result(mid, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo_text":
            t = str(args.get("text", ""))
            text = "echo: %s（长度 %d）" % (t, len(t))
        elif name == "add_numbers":
            try:
                text = "%d" % (int(args.get("a", 0)) + int(args.get("b", 0)))
            except Exception as e:
                error(mid, -32602, "参数不是整数: %s" % e)
                return
        else:
            error(mid, -32601, "未知工具: %s" % name)
            return
        result(mid, {"content": [{"type": "text", "text": text}], "isError": False})
    elif method is not None:
        error(mid, -32601, "不支持的方法: %s" % method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception as e:
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32700, "message": "解析失败: %s" % e}})


if __name__ == "__main__":
    main()

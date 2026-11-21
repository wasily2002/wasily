# -*- coding: utf-8 -*-
"""MCP HTTP 网关：让第三方程序用标准 JSON-RPC 接入本机能力总线。

端点：
    POST /mcp                     JSON-RPC 2.0（initialize / tools/list / tools/call / ping）
    POST /v1/mcp/register         注册第三方 MCP 服务（stdio 或 http）
    POST /v1/mcp/unregister       移除第三方服务
鉴权：config.json 的 mcp.gateway_token 为空时仅允许本机访问；
设置后要求 Authorization: Bearer <token>。
"""
import hmac
import json
import os

import config as cfg
import mcp_hub

PROTOCOL = "2024-11-05"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "mcp.json")


def _hub():
    return mcp_hub.active()


def _token():
    return str(cfg.get("mcp.gateway_token", "") or "")


def authorized(handler):
    token = _token()
    if not token:
        # 未配置令牌时只信任本机回环，避免局域网内任意调用工具
        return is_local(handler)
    header = handler.headers.get("Authorization") or ""
    return hmac.compare_digest(header, "Bearer " + token)


def _tools_list():
    hub = _hub()
    out = []
    for name, st in hub.servers.items():
        ns = mcp_hub._fmt_namespace(st["cfg"].get("namespace", None), name)  # noqa: SLF001
        for tool in st.get("tools") or []:
            raw = tool.get("name")
            if not raw:
                continue
            out.append({
                "name": ns + raw,
                "description": tool.get("description") or "",
                "inputSchema": tool.get("inputSchema")
                or {"type": "object", "properties": {}},
            })
    return out


def rpc(req):
    mid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    try:
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL,
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "wasily-capability-bus", "version": "1.0.0"}}
        elif method == "tools/list":
            result = {"tools": _tools_list()}
        elif method == "ping":
            result = {}
        elif method == "tools/call":
            full = str(params.get("name") or "")
            args = params.get("arguments") or {}
            meta = getattr(_hub(), "_names", {}).get(full)  # noqa: SLF001
            if not meta:
                raise ValueError("未知工具：%s" % full)
            server, raw, _readonly = meta
            text = _hub().call_tool(server, raw, args)
            is_error = text.startswith("[MCP 调用失败]") or text.startswith("[MCP 工具错误]")
            result = {"content": [{"type": "text", "text": text}],
                      "isError": is_error}
        else:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": "不支持的方法：%s" % method}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as exc:  # noqa: BLE001
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32603, "message": "%s: %s" % (type(exc).__name__, exc)}}


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"mcpServers": {}}


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def register_server(spec):
    if not isinstance(spec, dict) or not spec.get("name"):
        raise ValueError("缺少服务名 name")
    name = str(spec["name"]).strip()
    if name in ("self", "kb", "browser"):
        raise ValueError("内置服务名不可覆盖：%s" % name)
    transport = str(spec.get("transport") or "stdio")
    entry = {"_说明": str(spec.get("description") or "第三方 MCP 服务"),
             "transport": transport,
             "namespace": spec.get("namespace") or "mcp__%s__" % name,
             "enabled": bool(spec.get("enabled", True))}
    if transport == "stdio":
        if not spec.get("command"):
            raise ValueError("stdio 服务需要 command")
        entry["command"] = spec["command"]
        entry["args"] = spec.get("args") or []
        entry["cwd"] = spec.get("cwd") or "."
    else:
        if not spec.get("url"):
            raise ValueError("http 服务需要 url")
        entry["url"] = spec["url"]
    data = _read_json(CONFIG_PATH)
    data.setdefault("mcpServers", {})[name] = entry
    _write_json(CONFIG_PATH, data)
    _hub().reload_tools()
    return {"ok": True, "name": name, "server": entry,
            "servers": _hub().status()}


def unregister_server(name):
    name = str(name or "").strip()
    if name in ("self", "kb", "browser"):
        raise ValueError("内置服务不可移除：%s" % name)
    data = _read_json(CONFIG_PATH)
    servers = data.get("mcpServers") or {}
    if name not in servers:
        raise ValueError("服务不存在：%s" % name)
    servers.pop(name, None)
    _write_json(CONFIG_PATH, data)
    _hub().reload_tools()
    return {"ok": True, "removed": name, "servers": _hub().status()}


def is_local(handler):
    addr = (handler.client_address or [""])[0]
    return addr in ("127.0.0.1", "::1", "localhost")

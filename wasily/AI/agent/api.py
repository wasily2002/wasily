# -*- coding: utf-8 -*-
"""agent HTTP 接口：/v1/agent/*。

挂载方式与 site_api / apps_api 一致，由 api_server 在 do_GET / do_POST 中前置调用。
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs

import agent as _agent
from . import errors
from .errors import AgentError

GET_ROUTES = ("/v1/agent/status", "/v1/agent/registry", "/v1/agent/health",
              "/v1/agent/events", "/v1/agent/describe")
POST_ROUTES = ("/v1/agent/call", "/v1/agent/reload", "/v1/agent/policy")


def _q(query, key, default=""):
    vals = parse_qs(query or "").get(key)
    return vals[0] if vals else default


def handles_get(path):
    return path in GET_ROUTES


def handles_post(path):
    return path in POST_ROUTES


def handle_get(handler, path, query=""):
    if not handles_get(path):
        return False
    try:
        if path == "/v1/agent/status":
            handler._send_json(200, _agent.snapshot())
        elif path == "/v1/agent/registry":
            kind = _q(query, "kind") or None
            group = _q(query, "group") or None
            kw = _q(query, "q")
            items = _agent.search(kw, kind) if kw else _agent.resources(kind, group)
            handler._send_json(200, {"ok": True, "count": len(items),
                                     "items": items})
        elif path == "/v1/agent/health":
            name = _q(query, "name") or None
            handler._send_json(200, {"ok": True, "services": _agent.health(name)})
        elif path == "/v1/agent/events":
            after = _q(query, "after", "0")
            limit = _q(query, "limit", "50")
            handler._send_json(200, {"ok": True,
                                     "events": _agent.events(after, int(limit))})
        else:  # describe
            cap = _q(query, "capability")
            handler._send_json(200, {"ok": True,
                                     "resource": _agent.describe(cap)})
    except AgentError as e:
        handler._send_json(e.http_status, e.to_dict())
    except Exception as e:  # noqa: BLE001
        handler._send_json(500, errors.from_exception(e))
    return True


def handle_post(handler, path, data):
    if not handles_post(path):
        return False
    data = data if isinstance(data, dict) else {}
    try:
        if path == "/v1/agent/call":
            cap = str(data.get("capability") or "")
            args = data.get("args") or {}
            out = _agent.call(cap, args)
            status = 200
            if not out.get("ok"):
                code = (out.get("error") or {}).get("code", "invalid_argument")
                status = {"invalid_argument": 400, "not_found": 404,
                          "permission_denied": 403,
                          "dependency_missing": 409,
                          "upstream_error": 502}.get(code, 400)
            handler._send_json(status, out)
        elif path == "/v1/agent/reload":
            handler._send_json(200, {"ok": True,
                                     "result": _agent.reload_registry()})
        else:  # policy
            policy = _agent.set_policy(
                role=data.get("role"),
                roots=data.get("roots"),
                allow_mcp_write=data.get("allow_mcp_write"),
                persist=bool(data.get("persist", True)))
            handler._send_json(200, {"ok": True, "policy": policy})
    except AgentError as e:
        handler._send_json(e.http_status, e.to_dict())
    except Exception as e:  # noqa: BLE001
        handler._send_json(500, errors.from_exception(e))
    return True

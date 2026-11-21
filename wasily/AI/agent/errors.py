# -*- coding: utf-8 -*-
"""agent 统一管理层的错误类型。

所有对外接口都返回统一的错误信封，避免上层收到裸 KeyError / TypeError：
    {"ok": false, "error": {"code": "...", "message": "...", "detail": {...}}}
"""
from __future__ import annotations


class AgentError(Exception):
    """agent 层所有可预期错误的基类。"""

    code = "agent_error"
    http_status = 500

    def __init__(self, message="", detail=None):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.detail = detail or {}

    def to_dict(self):
        return {"ok": False,
                "error": {"code": self.code, "message": self.message,
                          "detail": self.detail}}


class InvalidArgument(AgentError):
    code = "invalid_argument"
    http_status = 400


class NotFound(AgentError):
    code = "not_found"
    http_status = 404


class PermissionDenied(AgentError):
    code = "permission_denied"
    http_status = 403


class DependencyMissing(AgentError):
    code = "dependency_missing"
    http_status = 409


class UpstreamError(AgentError):
    code = "upstream_error"
    http_status = 502


def envelope(result, ms=0):
    """成功结果的标准信封。"""
    return {"ok": True, "result": result, "ms": int(ms)}


def from_exception(exc, ms=0):
    """把任意异常转成错误信封；未知异常归一为 internal_error。"""
    if isinstance(exc, AgentError):
        out = exc.to_dict()
    else:
        out = {"ok": False,
               "error": {"code": "internal_error",
                         "message": "%s: %s" % (type(exc).__name__, exc),
                         "detail": {}}}
    out["ms"] = int(ms)
    return out

# -*- coding: utf-8 -*-
"""内置应用 HTTP 入口：/apps 页面 + 读写/抽帧/流媒体接口。

页面只是 kb_apps 的薄壳：所有操作最终仍然落到同一套函数，
因此 AI 通过 MCP 调用与用户在页面点击执行的是同一条链路。
"""
import mimetypes
import os
import re
from urllib.parse import parse_qs

import kb_apps

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(BASE_DIR, "apps.html")

POST_PATHS = ("/v1/apps/edit", "/v1/apps/video/frames", "/v1/apps/search")


def handles_post(path):
    return path in POST_PATHS


def _q(query, key, default=""):
    return (parse_qs(query or "").get(key) or [default])[0]


def _err(handler, exc):
    status = 400 if isinstance(exc, kb_apps.AppError) else 500
    handler._send_json(status, {"ok": False, "error": str(exc)})


def handle_get(handler, path, query=""):
    if path in ("/apps", "/apps/"):
        if not os.path.isfile(PAGE):
            handler._set_headers(404)
            handler.wfile.write(b"apps.html not found")
            return True
        handler._set_headers(200, "text/html; charset=utf-8")
        with open(PAGE, "r", encoding="utf-8") as f:
            handler.wfile.write(f.read().encode("utf-8"))
        return True

    if path == "/v1/apps/read":
        raw = _q(query, "path")
        mode = (_q(query, "mode", "auto") or "auto").lower()
        try:
            if mode in ("auto", "code", "text") and raw.lower().endswith(
                    tuple(kb_apps._CODE_EXTS)):  # noqa: SLF001
                out = kb_apps.code_read(raw, start=int(_q(query, "start", "1") or 1),
                                        end=int(_q(query, "end", "0") or 0))
            elif mode in ("auto", "office"):
                out = kb_apps.office_read(raw, sheet=_q(query, "sheet"),
                                          slide=_q(query, "slide"))
            elif mode == "video":
                out = kb_apps.video_info(raw)
            else:
                out = kb_apps.code_read(raw)
            handler._send_json(200, out)
        except Exception as exc:  # noqa: BLE001
            _err(handler, exc)
        return True

    if path == "/v1/apps/file":
        raw = _q(query, "path")
        try:
            full = kb_apps.resolve_path(raw)
        except Exception as exc:  # noqa: BLE001
            _err(handler, exc)
            return True
        if not os.path.isfile(full):
            handler._send_json(404, {"ok": False, "error": "文件不存在"})
            return True
        size = os.path.getsize(full)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        start, end = 0, size - 1
        status = 200
        m = re.match(r"bytes=(\d*)-(\d*)", handler.headers.get("Range") or "")
        if m:
            if m.group(1):
                start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), size - 1)
            if start > end or start >= size:
                handler.send_response(416)
                handler.send_header("Content-Range", "bytes */%d" % size)
                handler.end_headers()
                return True
            status = 206
        length = end - start + 1
        handler.send_response(status)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(length))
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Content-Type-Options", "nosniff")
        if status == 206:
            handler.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        handler.end_headers()
        try:
            with open(full, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True

    return False


def handle_post(handler, path, data):
    data = data or {}
    try:
        if path == "/v1/apps/edit":
            target = str(data.get("path") or "")
            ext = os.path.splitext(target)[1].lower()
            ops = data.get("operations") or []
            if data.get("kind") == "office" or ext in (".docx", ".xlsx", ".pptx"):
                out = kb_apps.office_edit(target, ops, dry_run=bool(data.get("dry_run")))
            else:
                out = kb_apps.code_edit(target, ops, dry_run=bool(data.get("dry_run")))
            handler._send_json(200, out)
            return True
        if path == "/v1/apps/video/frames":
            out = kb_apps.video_frames(str(data.get("path") or ""),
                                       count=int(data.get("count") or 6),
                                       width=int(data.get("width") or 640))
            handler._send_json(200, out)
            return True
        if path == "/v1/apps/search":
            out = kb_apps.code_search(str(data.get("query") or ""),
                                      root=str(data.get("root") or ""),
                                      limit=int(data.get("limit") or 50))
            handler._send_json(200, out)
            return True
    except Exception as exc:  # noqa: BLE001
        _err(handler, exc)
        return True
    return False

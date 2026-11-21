"""独立产品网站 + 站点级接口。

页面入口 /site，静态资源在 site/ 下；站点只做展示与聚合，不复制业务逻辑：
工作台仍走 /chat 与 /v1/agent/*，知识库仍走 /kb 与 /v1/kb/*。
"""
import json
import mimetypes
import os
import socket
import time

import config as cfg
import log_center
import update_service

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(BASE_DIR, "site")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

# 设置页允许读写的键；未列出的配置不通过站点暴露
SETTING_KEYS = (
    "logging.path", "logging.keep_days",
    "update.channel", "update.manifest_url", "update.auto_check",
    "ui.theme", "ui.language", "ide.startup_window", "network.proxy",
    "prompt_optimize.mode", "prompt_optimize.template", "prompt_optimize.max_chars",
    "web.auto_search", "web.engine", "task_router.auto", "task_router.preflight_notice",
)

POST_PATHS = (
    "/v1/settings/set", "/v1/logs/open", "/v1/logs/purge",
    "/v1/update/check", "/v1/update/download", "/v1/update/apply",
)
POST_PREFIXES = ("/v1/logs/", "/v1/update/", "/v1/settings/")


def handles_post(path):
    """先判定再读请求体：其他 POST 接口可能要自己读原始 body（上传/语音）。"""
    return path in POST_PATHS or path.startswith(POST_PREFIXES)


def _json(handler, status, payload):
    handler._send_json(status, payload)


def _safe_static(rel):
    rel = (rel or "index.html").lstrip("/").replace("\\", "/")
    full = os.path.realpath(os.path.join(SITE_DIR, rel))
    root = os.path.realpath(SITE_DIR)
    if full == root or not full.startswith(root + os.sep):
        return ""
    if os.path.isdir(full):
        full = os.path.join(full, "index.html")
    return full if os.path.isfile(full) else ""


def _serve_file(handler, full):
    ext = os.path.splitext(full)[1].lower()
    handler._set_headers(200, MIME.get(ext) or mimetypes.guess_type(full)[0]
                         or "application/octet-stream")
    with open(full, "rb") as f:
        handler.wfile.write(f.read())
    return True


def _port_open(port, timeout=0.25):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _mcp_summary():
    try:
        with open(os.path.join(BASE_DIR, "mcp.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        servers = data.get("mcpServers") or {}
        return [{"name": k, "transport": v.get("transport", "stdio"),
                 "enabled": bool(v.get("enabled", True))} for k, v in servers.items()]
    except (OSError, ValueError):
        return []


def _kb_summary():
    fp = os.path.join(BASE_DIR, "kb_data", "kb.db")
    size = os.path.getsize(fp) if os.path.isfile(fp) else 0
    return {"index_size": size, "page": "/kb", "api": "/v1/kb/status"}


def overview():
    cur = update_service.current()
    models = cfg.get("models", []) or []
    current_model = str(cfg.get("current_model", "") or "")
    model_name = ""
    for m in models:
        if isinstance(m, dict) and m.get("id") == current_model:
            model_name = m.get("name") or current_model
            break
    log_info = log_center.info()
    return {
        "ok": True,
        "product": cur.get("product") or "Wasily",
        "version": cur.get("version"),
        "build": cur.get("build") or "",
        "server_time": time.time(),
        "model": {"id": current_model, "name": model_name or current_model,
                  "count": len(models)},
        "kb": _kb_summary(),
        "logs": {"root": log_info["root"], "session": log_info["session"],
                 "default_root": log_info["default_root"],
                 "custom": log_info["custom"], "files": len(log_info["files"])},
        "update": {"current": cur.get("version"),
                   "last_check": cfg.get("update.last_check", 0),
                   "channel": cfg.get("update.channel", "stable")},
        "mcp": _mcp_summary(),
        "services": {"api": True, "ide_bridge": _port_open(18753),
                     "remote_mcp": _port_open(9000)},
    }


def handle_get(handler, path, query=""):
    """返回 True 表示已处理。"""
    if path in ("/site", "/site/"):
        return _serve_file(handler, os.path.join(SITE_DIR, "index.html"))
    if path.startswith("/site/"):
        full = _safe_static(path[len("/site/"):])
        if not full:
            _json(handler, 404, {"ok": False, "error": "资源不存在"})
            return True
        return _serve_file(handler, full)

    if path == "/v1/site/overview":
        _json(handler, 200, overview())
        return True

    if path == "/v1/logs/info":
        _json(handler, 200, dict(log_center.info(), ok=True))
        return True

    if path == "/v1/logs/tail":
        from urllib.parse import parse_qs
        qs = parse_qs(query or "")
        name = (qs.get("name") or ["api_server.log"])[0]
        lines = int((qs.get("lines") or ["200"])[0] or 200)
        session = (qs.get("session") or [""])[0]
        _json(handler, 200, {"ok": True, "name": name,
                             "text": log_center.tail(name, lines, session),
                             "path": log_center.resolve_file(name, session)})
        return True

    if path == "/v1/update/status":
        _json(handler, 200, dict(update_service.status(), ok=True))
        return True

    if path == "/v1/settings/all":
        data = {k: cfg.get(k) for k in SETTING_KEYS}
        _json(handler, 200, {"ok": True, "settings": data,
                             "defaults": {"logging.path": log_center.default_root()}})
        return True

    return False


def _set_setting(key, value):
    if key not in SETTING_KEYS:
        raise ValueError("不允许修改该项：%s" % key)
    if key == "update.manifest_url":
        value = str(value or "").strip()
    if key == "logging.path":
        return {"logging.path": cfg.get("logging.path", ""),
                "session_dir": log_center.set_root(value)}
    if key == "logging.keep_days":
        value = max(0, int(value or 0))
    if key in ("update.auto_check", "web.auto_search", "task_router.auto",
               "task_router.preflight_notice"):
        value = bool(value)
    if key in ("prompt_optimize.max_chars",):
        value = max(100, min(4000, int(value or 1200)))
    if key in ("prompt_optimize.mode", "web.engine"):
        value = str(value or "").strip().lower()
    if key in ("prompt_optimize.template", "update.manifest_url"):
        value = str(value or "").strip()
    cfg.set_value(key, value)
    cfg.save()
    if key.startswith("update."):
        update_service._CACHE.update(ts=0, data=None)  # noqa: SLF001 —— 同包内部缓存
    return {key: value}


def handle_post(handler, path, data):
    """返回 True 表示已处理。"""
    data = data or {}

    if path == "/v1/settings/set":
        key = str(data.get("key") or "").strip()
        try:
            changed = _set_setting(key, data.get("value"))
        except (ValueError, TypeError) as exc:
            _json(handler, 400, {"ok": False, "error": str(exc)})
            return True
        _json(handler, 200, {"ok": True, "changed": changed,
                             "settings": {k: cfg.get(k) for k in SETTING_KEYS}})
        return True

    if path == "/v1/logs/open":
        try:
            target = log_center.open_folder(data.get("path") or "")
        except Exception as exc:  # noqa: BLE001
            _json(handler, 500, {"ok": False, "error": str(exc)})
            return True
        _json(handler, 200, {"ok": True, "path": target})
        return True

    if path == "/v1/logs/purge":
        removed = log_center.purge_old(data.get("days"))
        _json(handler, 200, {"ok": True, "removed": removed})
        return True

    if path == "/v1/update/check":
        _json(handler, 200, update_service.check(force=bool(data.get("force", True))))
        return True

    if path == "/v1/update/download":
        try:
            result = update_service.download(data.get("package") or {})
        except Exception as exc:  # noqa: BLE001
            _json(handler, 500, {"ok": False, "error": str(exc)})
            return True
        _json(handler, 200, dict(result, ok=True))
        return True

    if path == "/v1/update/apply":
        try:
            pending = update_service.apply(data.get("path"), data.get("version"))
        except Exception as exc:  # noqa: BLE001
            _json(handler, 500, {"ok": False, "error": str(exc)})
            return True
        _json(handler, 200, {"ok": True, "pending": pending})
        return True

    return False

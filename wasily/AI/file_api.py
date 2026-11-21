# -*- coding: utf-8 -*-
"""文件管理器 HTTP 接口：/v1/files/*。

设计要点：
- 预览文本直接复用知识库的 kb_parser.extract，保证「文档查看」与「入库解析」
  走同一套抽取逻辑，不会出现预览和检索内容不一致。
- 浏览范围是全盘（盘符可切换），与知识库全盘扫描口径一致；写操作只有上传，
  且只取上传文件名的 basename 写入目标目录，杜绝路径穿越。
- 下载 / 原图走流式输出，避免大文件把整个服务进程吃满。
"""
from __future__ import annotations

import json
import os
import string
import time
from urllib.parse import parse_qs, quote, unquote

import kb_parser
import kb_store

MAX_PREVIEW_CHARS = 200_000
MAX_RAW_BYTES = 60 * 1024 * 1024       # 单文件原图预览上限 60MB
MAX_UPLOAD_BYTES = 200 * 1024 * 1024   # 单文件上传上限 200MB
CHUNK = 65536

GET_ROUTES = ("/v1/files/list", "/v1/files/stat", "/v1/files/preview",
              "/v1/files/raw", "/v1/files/download", "/v1/files/kb-status")
POST_ROUTES = ("/v1/files/upload",)


def handles_get(path: str) -> bool:
    return path in GET_ROUTES


def handles_post(path: str) -> bool:
    return path in POST_ROUTES


def _q(query: str, key: str, default: str = "") -> str:
    vals = parse_qs(query or "", keep_blank_values=True).get(key)
    return vals[0] if vals else default


def _norm(path: str) -> str:
    return os.path.abspath(unquote(path or ""))


def _drives() -> list[str]:
    """列出 Windows 上存在的盘符；非 Windows 返回根目录。"""
    if os.name != "nt":
        return ["/"]
    out = []
    for letter in string.ascii_uppercase:
        root = letter + ":\\"
        try:
            if os.path.exists(root):
                out.append(root)
        except OSError:
            continue
    return out


def _kind_of(path: str) -> str:
    try:
        k = kb_parser.classify(path)
    except Exception:  # noqa: BLE001
        k = "skip"
    return k if k and k != "skip" else "other"


def _entry(full: str, name: str) -> dict:
    try:
        st = os.stat(full)
        is_dir = os.path.isdir(full)
    except OSError:
        return {"name": name, "path": full, "is_dir": False, "size": 0,
                "mtime": 0, "ext": "", "kind": "other", "readable": False}
    ext = os.path.splitext(name)[1].lower()
    return {
        "name": name, "path": full, "is_dir": is_dir,
        "size": 0 if is_dir else int(st.st_size),
        "mtime": int(st.st_mtime),
        "ext": ext, "kind": "folder" if is_dir else _kind_of(full),
        "readable": True,
    }


def _kb_state(path: str) -> dict:
    """查知识库登记状态：已入库 / 未入库 / 错误。"""
    try:
        rec = kb_store.get_store().get_file_by_path(path)
    except Exception:  # noqa: BLE001
        return {"indexed": False, "status": "unknown", "kind": ""}
    if not rec:
        return {"indexed": False, "status": "absent", "kind": ""}
    status = rec.get("status") or ""
    return {"indexed": status == "indexed", "status": status,
            "kind": rec.get("kind") or "", "id": rec.get("id")}


def _send(handler, obj: dict, status: int = 200) -> bool:
    handler._send_json(status, obj)  # noqa: SLF001
    return True


# ---------- GET ----------

def handle_get(handler, path: str, query: str = "") -> bool:
    if not handles_get(path):
        return False
    if path == "/v1/files/list":
        return _list(handler, _q(query, "path"))
    if path == "/v1/files/stat":
        return _stat(handler, _q(query, "path"))
    if path == "/v1/files/preview":
        return _preview(handler, _q(query, "path"))
    if path == "/v1/files/raw":
        return _raw(handler, _q(query, "path"))
    if path == "/v1/files/download":
        return _download(handler, _q(query, "path"))
    if path == "/v1/files/kb-status":
        target = _norm(_q(query, "path"))
        return _send(handler, {"ok": True, "path": target, "kb": _kb_state(target)})
    return False


def _list(handler, raw_path: str) -> bool:
    target = _norm(raw_path) if raw_path else ""
    if not target or not os.path.isdir(target):
        # 没有合法目录时返回盘符列表，前端可先选盘再下钻
        return _send(handler, {"ok": True, "path": target, "is_drive_root": not target,
                               "parent": "", "drives": _drives(), "entries": []})
    entries = []
    try:
        with os.scandir(target) as it:
            for e in it:
                try:
                    entries.append(_entry(e.path, e.name))
                except OSError:
                    continue
    except PermissionError:
        return _send(handler, {"ok": False, "error": "没有权限访问该目录",
                               "path": target}, 403)
    except OSError as exc:
        return _send(handler, {"ok": False, "error": "读取目录失败: %s" % exc,
                               "path": target}, 400)
    # 文件夹在前，再按名称；不区分大小写，中文按默认排序可接受
    entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    parent = os.path.dirname(target.rstrip("\\/")) or target
    return _send(handler, {"ok": True, "path": target, "parent": parent,
                           "drives": _drives(), "entries": entries})


def _stat(handler, raw_path: str) -> bool:
    target = _norm(raw_path)
    if not os.path.exists(target):
        return _send(handler, {"ok": False, "error": "路径不存在", "path": target}, 404)
    name = os.path.basename(target) or target
    return _send(handler, {"ok": True, "entry": _entry(target, name),
                           "kb": _kb_state(target)})


def _preview(handler, raw_path: str) -> bool:
    target = _norm(raw_path)
    if not os.path.isfile(target):
        return _send(handler, {"ok": False, "error": "文件不存在", "path": target}, 404)
    size = os.path.getsize(target)
    kind = _kind_of(target)
    if kind == "image":
        return _send(handler, {"ok": True, "path": target, "kind": "image",
                               "size": size, "raw_url": "/v1/files/raw?path=" + quote(target),
                               "kb": _kb_state(target)})
    if kind == "other":
        return _send(handler, {"ok": True, "path": target, "kind": "other",
                               "size": size, "text": "",
                               "note": "该类型暂不支持在线预览，请下载后查看",
                               "kb": _kb_state(target)})
    try:
        res = kb_parser.extract(target, kind, allow_caption=False)
    except Exception as exc:  # noqa: BLE001
        return _send(handler, {"ok": False, "error": "解析失败: %s" % exc,
                               "path": target, "kind": kind}, 400)
    text = (res.get("text") or "")[:MAX_PREVIEW_CHARS]
    return _send(handler, {"ok": True, "path": target, "kind": kind, "size": size,
                           "text": text, "note": res.get("note") or "",
                           "kb": _kb_state(target)})


def _open_for_stream(handler, target: str, download: bool):
    if not os.path.isfile(target):
        handler._send_json(404, {"ok": False, "error": "文件不存在"})  # noqa: SLF001
        return None
    size = os.path.getsize(target)
    if not download and size > MAX_RAW_BYTES:
        handler._send_json(400, {"ok": False,  # noqa: SLF001
                                 "error": "文件过大（%dMB），请下载后查看" % (size // 1048576)})
        return None
    import mimetypes
    ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
    name = os.path.basename(target)
    try:
        f = open(target, "rb")
    except OSError as exc:
        handler._send_json(400, {"ok": False, "error": "无法打开文件: %s" % exc})  # noqa: SLF001
        return None
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(size))
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Access-Control-Allow-Origin", "*")
    if download:
        import re as _re
        stem, ext = os.path.splitext(name)
        safe = (_re.sub(r"[^A-Za-z0-9._-]", "", stem) or "file") + (ext or "")
        handler.send_header("Content-Disposition",
                            'attachment; filename="' + safe.replace('"', '') +
                            '"; filename*=UTF-8\'\'' + quote(name, safe=''))
    handler.end_headers()
    return f


def _raw(handler, raw_path: str) -> bool:
    f = _open_for_stream(handler, _norm(raw_path), download=False)
    if f is None:
        return True
    # HEAD 只需首部：_open_for_stream 已发出正确的 Content-Length/Content-Type，
    # 不必再把文件内容读一遍（全局 _HeadWfile 也会丢弃响应体，这里仅省磁盘 IO）
    if handler.command == "HEAD":
        f.close()
        return True
    try:
        while True:
            buf = f.read(CHUNK)
            if not buf:
                break
            handler.wfile.write(buf)
    finally:
        f.close()
    return True


def _download(handler, raw_path: str) -> bool:
    f = _open_for_stream(handler, _norm(raw_path), download=True)
    if f is None:
        return True
    # HEAD 只需首部：Content-Length / Content-Disposition 已发出，跳过文件内容读取
    if handler.command == "HEAD":
        f.close()
        return True
    try:
        while True:
            buf = f.read(CHUNK)
            if not buf:
                break
            handler.wfile.write(buf)
    finally:
        f.close()
    return True


# ---------- POST ----------

def handle_post(handler, path: str) -> bool:
    """上传单独走原始 body 解析，其余 POST 仍可用 JSON。"""
    if not handles_post(path):
        return False
    body = _read_body(handler)
    if path == "/v1/files/upload":
        return _upload(handler, body)
    return False


def _read_body(handler) -> bytes:
    try:
        n = int(handler.headers.get("Content-Length", 0))
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return b""
    if n > MAX_UPLOAD_BYTES:
        handler._send_json(400, {"ok": False,  # noqa: SLF001
                                 "error": "上传内容超过 %dMB 限制" % (MAX_UPLOAD_BYTES // 1048576)})
        return b""
    return handler.rfile.read(n)


def _parse_multipart(body: bytes, boundary: str) -> list[dict]:
    """极简 multipart/form-data 解析：返回 [{name, filename, data}]。"""
    sep = ("--" + boundary).encode()
    parts = body.split(sep)
    out = []
    for part in parts:
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        head, data = part.split(b"\r\n\r\n", 1)
        # data 末尾通常带 \r\n（分隔前的换行），去掉
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers = head.decode("utf-8", "replace").split("\r\n")
        name = filename = ""
        for line in headers:
            if line.lower().startswith("content-disposition"):
                import re as _re
                m = _re.search(r'name="([^"]*)"', line)
                if m:
                    name = m.group(1)
                m = _re.search(r'filename="([^"]*)"', line)
                if m:
                    filename = m.group(1)
        if name:
            out.append({"name": name, "filename": filename, "data": data})
    return out


def _upload(handler, body: bytes) -> bool:
    ctype = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in ctype or "boundary=" not in ctype:
        return _send(handler, {"ok": False, "error": "需要 multipart/form-data 上传"}, 400)
    boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
    fields = _parse_multipart(body, boundary)
    target_dir = ""
    files = []
    for f in fields:
        if f["name"] == "dir" and not f["filename"]:
            target_dir = _norm(f["data"].decode("utf-8", "replace"))
        elif f["name"] in ("file", "files") and f["filename"]:
            files.append(f)
    if not target_dir or not os.path.isdir(target_dir):
        return _send(handler, {"ok": False, "error": "目标目录不存在"}, 400)
    saved = []
    for f in files:
        name = os.path.basename(f["filename"])  # 只取 basename，杜绝穿越
        if not name:
            continue
        dest = os.path.join(target_dir, name)
        with open(dest, "wb") as out:
            out.write(f["data"])
        saved.append({"name": name, "size": len(f["data"]), "path": dest})
    if not saved:
        return _send(handler, {"ok": False, "error": "没有收到文件"}, 400)
    return _send(handler, {"ok": True, "dir": target_dir, "saved": saved})

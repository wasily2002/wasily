# -*- coding: utf-8 -*-
"""知识库 HTTP 接口（挂到 api_server 的 /kb 与 /v1/kb/*）。

安全边界：/v1/kb/file 与 /v1/kb/thumb 只接受“已登记进知识库”的路径，
路径必须能在 files 表命中，避免变成任意文件读取接口。
"""
import hashlib
import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import parse_qs

import kb_search
import kb_store
import kb_vlm

BASE_DIR = Path(__file__).resolve().parent
THUMB_DIR = Path(os.environ.get("WASILY_KB_DIR") or (BASE_DIR / "kb_data")) / "thumbs"


def _q(query, key, default=""):
    vals = parse_qs(query or "").get(key)
    return vals[0] if vals else default


def _thumb(path):
    """生成并缓存缩略图，返回 jpeg 字节；失败返回 None。"""
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(("%s|%s" % (path, os.path.getmtime(path))).encode(
        "utf-8", "ignore")).hexdigest()
    fp = THUMB_DIR / (key + ".jpg")
    if fp.exists():
        return fp.read_bytes()
    try:
        from PIL import Image
        img = Image.open(path)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((360, 360))
        img.save(str(fp), "JPEG", quality=82)
        return fp.read_bytes()
    except Exception:  # noqa: BLE001 缩略图失败不影响主流程
        return None


def handle_get(handler, path, query):
    """返回 True 表示已处理该请求。"""
    store = kb_store.get_store()
    if path == "/kb":
        fp = BASE_DIR / "kb.html"
        if not fp.exists():
            handler._set_headers(404)
            handler.wfile.write(b"kb.html not found")
            return True
        handler._set_headers(200, "text/html; charset=utf-8")
        handler.wfile.write(fp.read_text(encoding="utf-8").encode("utf-8"))
        return True

    if path == "/v1/kb/status":
        import kb_index
        kb_search.warm_vector_cache()   # 页面轮询期间顺便预热向量，首次检索不再等
        handler._send_json(200, {"ok": True, "index": kb_index.get_indexer().status(),
                                 "vision": kb_vlm.status()})
        return True

    if path == "/v1/kb/stats":
        handler._send_json(200, {"ok": True, "stats": kb_search.stats()})
        return True

    if path == "/v1/kb/recent":
        rows = store.list_recent(int(_q(query, "limit", "30") or 30),
                                 kind=_q(query, "kind") or None)
        handler._send_json(200, {"ok": True, "files": rows})
        return True

    if path == "/v1/kb/search":
        res = kb_search.search(_q(query, "q"), kind=_q(query, "kind") or None,
                               limit=int(_q(query, "limit", "10") or 10))
        handler._send_json(200, res)
        return True

    if path in ("/v1/kb/file", "/v1/kb/thumb"):
        raw = _q(query, "path")
        rec = store.get_file_by_path(raw) if raw else None
        if not rec:
            handler._send_json(404, {"ok": False, "error": "该路径不在知识库索引中"})
            return True
        fp = rec["path"]
        if not os.path.isfile(fp):
            handler._send_json(404, {"ok": False, "error": "源文件已不存在"})
            return True
        if path == "/v1/kb/thumb":
            data = _thumb(fp)
            if data is None:
                handler._set_headers(415, "text/plain; charset=utf-8")
                handler.wfile.write("缩略图不可用".encode("utf-8"))
                return True
            handler._set_headers(200, "image/jpeg")
            handler.wfile.write(data)
            return True
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        size = os.path.getsize(fp)
        handler.send_response(200)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(size))
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()
        with open(fp, "rb") as f:
            while True:
                buf = f.read(65536)
                if not buf:
                    break
                handler.wfile.write(buf)
        return True
    return False


def handle_post(handler, path, data):
    if path == "/v1/kb/scan":
        import kb_index
        roots = data.get("roots")
        res = kb_index.get_indexer().start(roots=roots, force=bool(data.get("force")))
        handler._send_json(200 if res.get("ok") else 409, res)
        return True
    if path == "/v1/kb/stop":
        import kb_index
        handler._send_json(200, kb_index.get_indexer().stop())
        return True
    if path == "/v1/kb/download_model":
        started = kb_vlm.download_async()
        handler._send_json(200, {"ok": True, "started": started,
                                 "model": kb_vlm.model_info()})
        return True
    return False

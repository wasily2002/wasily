# -*- coding: utf-8 -*-
"""
文档站独立服务（docs_portal 版）
仅提供项目文档站相关路由：
  GET  /              → docs.html（无 project 参数时前端自动显示项目卡片入口）
  GET  /docs          → 同上（兼容旧 URL）
  GET  /docs?project=gxs   → 注入 docs.json + window.__DOC_PROJECT__
  GET  /api/projects       → 项目列表 manifest.json
  GET  /api/docs?project=xxx   → 读取对应项目 docs.json
  POST /api/docs?project=xxx   → 保存（临时文件 + .bak + os.replace 原子替换）
  GET  /files/<project>/<filename>  → 附件下载（basename 防穿越 + RFC 5987 中文文件名）
无任何 wasily AI 依赖（secure_store / tools / config / audit 等均不引用）。
"""
import os
import sys
import re
import json
import time
import shutil
import mimetypes
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "docs.html")
PROJECTS_DIR = os.path.join(BASE_DIR, "projects")
MANIFEST_PATH = os.path.join(PROJECTS_DIR, "manifest.json")

# 默认端口 8001（避免与 wasily AI 主服务 8000 冲突）
PORT = int(os.environ.get("DOCS_PORT", "8001"))
HOST = "0.0.0.0"


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def get_project_doc_path(project):
    """projects/<project>/docs.json；不存在则回退 BASE_DIR/docs.json（兼容 wasily）"""
    safe = (project or "").strip()
    if re.match(r'^[a-zA-Z0-9_-]+$', safe):
        sub = os.path.join(PROJECTS_DIR, safe, "docs.json")
        if os.path.exists(sub):
            return sub
    root_fallback = os.path.join(BASE_DIR, "docs.json")
    if os.path.exists(root_fallback):
        return root_fallback
    return None


class Handler(BaseHTTPRequestHandler):
    def _set_headers(self, status=200, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_OPTIONS(self):
        self._set_headers()

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} - {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        try:
            if path.startswith("/files/"):
                self._handle_files(path, qs)
            elif path == "/api/projects":
                self._handle_api_projects()
            elif path == "/api/docs":
                self._handle_api_docs_get(qs)
            elif path in ("/", "/portal"):
                self._serve_static("portal.html")
            elif path == "/docs":
                self._handle_docs_page(qs)
            elif path in ("/flowviz",):
                self._serve_static("flowviz.html")
            else:
                self._serve_static(path.lstrip("/"))
        except Exception as e:
            log(f"ERR GET {path}: {e}")
            self._set_headers(500, "text/plain; charset=utf-8")
            self.wfile.write(f"服务器内部错误: {e}".encode("utf-8", "ignore"))

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path == "/api/docs":
                self._handle_api_docs_post(qs)
            else:
                self._set_headers(404)
                self.wfile.write(json.dumps({"ok": False, "error": "not found"}).encode())
        except Exception as e:
            log(f"ERR POST {path}: {e}")
            self._set_headers(500)
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8", "ignore"))

    # ---------- 路由实现 ----------

    def _serve_static(self, relname):
        fp = os.path.join(BASE_DIR, relname)
        if os.path.exists(fp) and os.path.isfile(fp):
            ctype = mimetypes.guess_type(fp)[0] or "text/html; charset=utf-8"
            with open(fp, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        else:
            self._set_headers(404)
            self.wfile.write(f"{relname} not found".encode())

    def _handle_docs_page(self, qs):
        project = (qs.get("project") or [""])[0] or ""
        html_path = HTML_PATH
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                html = f.read()
            if project:
                doc_path = get_project_doc_path(project)
                doc_raw = ""
                if doc_path and os.path.exists(doc_path):
                    with open(doc_path, "r", encoding="utf-8") as f:
                        doc_raw = f.read()
                if doc_raw:
                    html = html.replace(
                        "<!--INJECT_DOCS-->",
                        '<script id="docData" type="application/json">'
                        + doc_raw.replace("</", "<\\/") + "</script>",
                    )
                html = html.replace(
                    "<!--PROJECT_SLUG-->",
                    '<script>window.__DOC_PROJECT__ = "' + project.replace('"', '\\"') + '";</script>',
                )
            self._set_headers(200, "text/html; charset=utf-8")
            self.wfile.write(html.encode("utf-8"))
        except Exception as e:
            self._set_headers(500, "text/plain; charset=utf-8")
            self.wfile.write(f"docs load error: {e}".encode("utf-8", "ignore"))

    def _handle_api_projects(self):
        self._set_headers()
        if os.path.exists(MANIFEST_PATH):
            with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
                self.wfile.write(f.read().encode("utf-8"))
        else:
            self.wfile.write(json.dumps([], ensure_ascii=False).encode("utf-8"))

    def _handle_api_docs_get(self, qs):
        project = (qs.get("project") or [""])[0] or "wasily"
        doc_path = get_project_doc_path(project)
        self._set_headers()
        if doc_path and os.path.exists(doc_path):
            with open(doc_path, "r", encoding="utf-8") as f:
                self.wfile.write(f.read().encode("utf-8"))
        else:
            self.wfile.write(json.dumps(
                {"site": {"title": project + " 项目文档", "logo": "W"},
                 "sections": []}, ensure_ascii=False).encode("utf-8"))

    def _handle_api_docs_post(self, qs):
        project = (qs.get("project") or [""])[0] or ""
        # 读请求体
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(n).decode("utf-8") if n else ""
        try:
            data = json.loads(body) if body else {}
        except Exception:
            self._set_headers(400)
            self.wfile.write(json.dumps({"ok": False, "error": "json 解析失败"}).encode())
            return
        if not isinstance(data.get("sections"), list):
            self._set_headers(400)
            self.wfile.write(json.dumps({"ok": False, "error": "缺少 sections 数组"}).encode())
            return
        # 确定目标路径
        if project and re.match(r'^[a-zA-Z0-9_-]+$', project):
            proj_dir = os.path.join(PROJECTS_DIR, project)
            os.makedirs(proj_dir, exist_ok=True)
            doc_path = os.path.join(proj_dir, "docs.json")
        else:
            doc_path = os.path.join(BASE_DIR, "docs.json")
        tmp_path = doc_path + ".tmp"
        try:
            if os.path.exists(doc_path):
                shutil.copy2(doc_path, doc_path + ".bak")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, doc_path)
            self._set_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())
            log(f"SAVE {doc_path}")
        except Exception as e:
            self._set_headers(500)
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8", "ignore"))

    def _handle_files(self, path, qs):
        """/files/<project>/<filename> 附件下载"""
        p = urlparse(self.path).path
        parts = p.split("/")
        if len(parts) < 3 or not parts[2]:
            self._set_headers(404); self.wfile.write(b"file not found"); return
        project = parts[2]
        if not re.match(r'^[a-zA-Z0-9_-]+$', project):
            self._set_headers(403); self.wfile.write(b"forbidden project"); return
        rel = "/".join(parts[3:])
        if not rel:
            self._set_headers(404); self.wfile.write(b"file not found"); return
        file_name = os.path.basename(unquote(rel))
        if not file_name or file_name in ("", ".", ".."):
            self._set_headers(403); self.wfile.write(b"forbidden filename"); return
        fp = os.path.join(PROJECTS_DIR, project, "files", file_name)
        # 安全校验：realpath 必须在允许目录内，防止 .. 穿越
        real = os.path.realpath(fp)
        allowed_root = os.path.realpath(os.path.join(PROJECTS_DIR, project, "files"))
        if not real.startswith(allowed_root + os.sep) and real != allowed_root:
            self._set_headers(403); self.wfile.write(b"forbidden path traversal"); return
        if not os.path.exists(fp) or not os.path.isfile(fp):
            self._set_headers(404); self.wfile.write(b"file not found"); return
        # 响应
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        size = os.path.getsize(fp)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        # RFC 5987 中文文件名
        _n, _e = os.path.splitext(file_name)
        safe_ascii = (re.sub(r'[^A-Za-z0-9._-]', '', _n) or 'file') + (_e or '')
        self.send_header("Content-Disposition",
            'attachment; filename="' + safe_ascii.replace('"', '') + '"; filename*=UTF-8\'\'' + quote(file_name, safe=''))
        self.end_headers()
        with open(fp, "rb") as f:
            self.wfile.write(f.read())


def main():
    if not os.path.exists(PROJECTS_DIR):
        os.makedirs(PROJECTS_DIR, exist_ok=True)
    if not os.path.exists(HTML_PATH):
        print("ERROR: docs.html not found alongside this server.py")
        sys.exit(1)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("=" * 56)
    print(f"  文档站独立服务启动")
    print(f"  地址:  http://127.0.0.1:{PORT}/")
    print(f"  文档:  http://127.0.0.1:{PORT}/docs?project=gxs  (供销社)")
    print(f"  文档:  http://127.0.0.1:{PORT}/docs?project=wasily (wasily AI)")
    print(f"  项目列表: http://127.0.0.1:{PORT}/api/projects")
    print(f"  工作目录: {BASE_DIR}")
    print(f"  按 Ctrl+C 停止")
    print("=" * 56, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()

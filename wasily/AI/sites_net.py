# -*- coding: utf-8 -*-
"""我的站点：把本地目录挂成独立的 HTTP 站点，并提供可选的内网穿透。

- 每个站点独立端口（8123+），绑 0.0.0.0，局域网可用 http://<本机IP>:<端口> 访问。
- 内网穿透用开源的 cloudflared quick tunnel（无需账号，类似花生壳）；
  未安装时给出可下载入口，下载的是官方 GitHub Release 的二进制。
"""
import json
import os
import re
import socket
import subprocess
import threading
import time
import uuid
import shutil
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(BASE_DIR, "sites_store.json")
BIN_DIR = os.path.join(BASE_DIR, "bin")
CF_EXE = os.path.join(BIN_DIR, "cloudflared.exe")
DEFAULT_DIR = os.path.join(os.path.dirname(os.path.dirname(BASE_DIR)), "待办事项网页")

_lock = threading.Lock()
_runtimes = {}   # sid -> {"httpd", "thread", "proc", "public_url"}
_pending = set()  # 正在建立映射的站点，避免并发重复拉起 cloudflared
_stats = {}      # sid -> {"date","hits","bytes","hourly"[24],"hourly_bytes"[24],"qps_sec","sec","qps_peak"}
_limits = {}     # sid -> {"daily_gb","qps"}
_logs = {}       # sid -> [{"t","m","p","s","b"}] 最近 300 条
_last_flush = {}
CF_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def _today():
    return time.strftime("%Y-%m-%d")


def _stats_for(sid):
    st = _stats.get(sid)
    if not st or st.get("date") != _today():
        st = {"date": _today(), "hits": 0, "bytes": 0,
              "hourly": [0] * 24, "hourly_bytes": [0] * 24,
              "qps_sec": 0, "sec": 0, "qps_peak": 0,
              "daily": (st or {}).get("daily", {})}
        _stats[sid] = st
    return st


def _snap_daily(st):
    """把当天累计量写进按天的历史，供观测数据切换日期范围。"""
    d = st.setdefault("daily", {})
    d[st["date"]] = {"hits": st["hits"], "bytes": st["bytes"],
                     "hourly": list(st["hourly"]),
                     "hourly_bytes": list(st["hourly_bytes"])}
    if len(d) > 7:
        for old in sorted(d)[:-7]:
            d.pop(old, None)


def _flush_stats(sid):
    st = _stats.get(sid)
    if not st:
        return
    try:
        with _lock:
            items = _load()
            for it in items:
                if it["id"] == sid:
                    it["stats"] = {k: st.get(k) for k in
                                   ("date", "hits", "bytes", "hourly",
                                    "hourly_bytes", "qps_peak", "daily")}
                    _save(items)
                    break
    except Exception:
        pass


class SiteError(Exception):
    pass


class _Handler(SimpleHTTPRequestHandler):
    """静态站点处理器：统计流量、限流、记录访问日志。"""

    def __init__(self, *args, directory=None, site_id="", **kwargs):
        # site_id 由 partial 注入，SimpleHTTPRequestHandler 不认识这个参数
        self.site_id = site_id
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, *args):
        pass

    def end_headers(self):
        # 本地站点频繁改文件，禁用缓存避免部署后还看到旧页面
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_response(self, code, message=None):
        self._status = code
        super().send_response(code, message)

    def send_header(self, keyword, value):
        if keyword.lower() == "content-length":
            try:
                self._clen = int(value)
            except Exception:
                self._clen = 0
        super().send_header(keyword, value)

    def _begin(self):
        sid = getattr(self, "site_id", "")
        self._sid = sid
        self._clen = 0
        self._status = 200
        if not sid:
            return
        st = _stats_for(sid)
        now = time.time()
        st["hits"] += 1
        st["hourly"][time.localtime(now).tm_hour] += 1
        sec = int(now)
        if st["sec"] != sec:
            st["sec"] = sec
            st["qps_sec"] = 0
        st["qps_sec"] += 1
        if st["qps_sec"] > st["qps_peak"]:
            st["qps_peak"] = st["qps_sec"]

    def _allowed(self):
        lim = _limits.get(self._sid) or {}
        st = _stats_for(self._sid)
        if lim.get("qps") and st["qps_sec"] > int(lim["qps"]):
            return False, "QPS 超过上限"
        if lim.get("daily_gb") and st["bytes"] > int(lim["daily_gb"]) * 1024 ** 3:
            return False, "单日流量超过上限"
        return True, ""

    def _finish(self):
        sid = getattr(self, "_sid", "")
        if not sid:
            return
        st = _stats_for(sid)
        n = self._clen if 200 <= getattr(self, "_status", 0) < 300 else 0
        if n > 0:
            st["bytes"] += n
            st["hourly_bytes"][time.localtime().tm_hour] += n
        _snap_daily(st)
        _logs.setdefault(sid, []).append({
            "t": time.time(), "m": self.command, "p": getattr(self, "path", ""),
            "s": getattr(self, "_status", 0), "b": n})
        if len(_logs[sid]) > 300:
            del _logs[sid][:-300]
        if time.time() - _last_flush.get(sid, 0) > 15:
            _last_flush[sid] = time.time()
            threading.Thread(target=_flush_stats, args=(sid,), daemon=True).start()

    def _serve(self, fn):
        self._begin()
        ok, why = self._allowed()
        if not ok:
            self.send_error(429, why)
            self._finish()
            return
        fn()
        self._finish()

    def do_GET(self):
        self._serve(lambda: super(_Handler, self).do_GET())

    def do_HEAD(self):
        self._serve(lambda: super(_Handler, self).do_HEAD())


def _load():
    try:
        with open(STORE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save(items):
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE)


def lan_ip():
    """取本机在局域网里的地址；取不到就退回 127.0.0.1"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
    except OSError:
        return False
    finally:
        s.close()
    # Windows 下 0.0.0.0 能与已占用的 127.0.0.1 重叠，再探测一次才算真空闲
    c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    c.settimeout(0.3)
    try:
        c.connect(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        c.close()


def _pick_port():
    for p in range(8300, 8400):
        if _port_free(p):
            return p
    raise SiteError("没有可用端口（8300-8399 都被占用）")


class _SiteServer(ThreadingHTTPServer):
    # 关掉地址复用，避免和用户自己起的服务抢同一个端口
    allow_reuse_address = False
    daemon_threads = True


def _start_server(item):
    sid = item["id"]
    rt = _runtimes.get(sid)
    if rt and rt.get("httpd"):
        return rt
    path = item.get("path") or ""
    if not os.path.isdir(path):
        raise SiteError("站点目录不存在：" + path)
    port = int(item.get("port") or 0)
    if not port or not _port_free(port):
        port = _pick_port()
        item["port"] = port    # 统计与限额：优先恢复今天的数据，跨天则清零
    saved = item.get("stats") or {}
    if sid not in _stats:
        if saved.get("date") == _today():
            _stats[sid] = {"date": saved["date"],
                           "hits": int(saved.get("hits") or 0),
                           "bytes": int(saved.get("bytes") or 0),
                           "hourly": saved.get("hourly") or [0] * 24,
                           "hourly_bytes": saved.get("hourly_bytes") or [0] * 24,
                           "qps_sec": 0, "sec": 0,
                           "qps_peak": int(saved.get("qps_peak") or 0),
                           "daily": saved.get("daily") or {}}
        else:
            _stats[sid] = _stats_for(sid)
            _stats[sid]["daily"] = saved.get("daily") or {}
    _limits[sid] = {"daily_gb": int(item.get("daily_limit_gb") or 10),
                    "qps": int(item.get("qps_limit") or 100)}
    httpd = _SiteServer(("0.0.0.0", port),
                        partial(_Handler, directory=path, site_id=sid))
    th = threading.Thread(target=httpd.serve_forever, daemon=True,
                          name="site-%s" % sid)
    th.start()
    rt = {"httpd": httpd, "thread": th, "proc": None, "public_url": ""}
    _runtimes[sid] = rt
    return rt


def _stop_server(sid):
    rt = _runtimes.pop(sid, None)
    if not rt:
        return
    if rt.get("proc"):
        try:
            rt["proc"].terminate()
        except Exception:
            pass
    try:
        rt["httpd"].shutdown()
        rt["httpd"].server_close()
    except Exception:
        pass


def view(item):
    rt = _runtimes.get(item["id"]) or {}
    running = bool(rt.get("httpd"))
    ip = lan_ip()
    port = item.get("port")
    st = _stats.get(item["id"]) or {}
    return {
        "id": item["id"],
        "name": item.get("name") or "未命名站点",
        "path": item.get("path") or "",
        "port": port,
        "status": "running" if running else "stopped",
        "enabled": bool(item.get("enabled", True)),
        "visibility": item.get("visibility") or "public",
        "created": item.get("created") or 0,
        "url_local": ("http://127.0.0.1:%s/" % port) if running else "",
        "url_lan": ("http://%s:%s/" % (ip, port)) if running else "",
        "url_domain": ("http://%s:%s/" % (item["custom_domain"], port))
                      if (item.get("custom_domain") and port) else "",
        "public_url": rt.get("public_url") or "",
        "tunnel": bool(rt.get("public_url")),
        "mapping": bool(item.get("mapping")),
        "custom_domain": item.get("custom_domain") or "",
        "views": st.get("hits", 0),
        "bytes": st.get("bytes", 0),
        "qps_peak": st.get("qps_peak", 0),
        "hourly": st.get("hourly") or [0] * 24,
        "hourly_bytes": st.get("hourly_bytes") or [0] * 24,
        "daily": st.get("daily") or {},
        "daily_limit_gb": int(item.get("daily_limit_gb") or 10),
        "qps_limit": int(item.get("qps_limit") or 100),
        "deploys": item.get("deploys") or [],
    }


def detail(sid):
    items = _load()
    item = next((it for it in items if it["id"] == sid), None)
    if not item:
        raise SiteError("站点不存在")
    return view(item)


def logs_list(sid):
    """最近访问日志（新的在前）。"""
    return list(reversed(_logs.get(sid, [])))[:200]


def set_limits(sid, daily_gb=None, qps=None):
    with _lock:
        items = _load()
        item = next((it for it in items if it["id"] == sid), None)
        if not item:
            raise SiteError("站点不存在")
        if daily_gb not in (None, ""):
            item["daily_limit_gb"] = max(1, min(1024, int(float(daily_gb))))
        if qps not in (None, ""):
            item["qps_limit"] = max(1, min(10000, int(float(qps))))
        _limits[sid] = {"daily_gb": int(item.get("daily_limit_gb") or 10),
                        "qps": int(item.get("qps_limit") or 100)}
        _save(items)
        return view(item)


def set_domain(sid, domain):
    """绑定自有域名（购买域名后把 DNS 解析到本机即可）。"""
    domain = str(domain or "").strip().lower()
    if domain and not re.match(r"^[a-z0-9][a-z0-9.-]{2,80}\.[a-z]{2,10}$", domain):
        raise SiteError("域名格式不正确")
    with _lock:
        items = _load()
        item = next((it for it in items if it["id"] == sid), None)
        if not item:
            raise SiteError("站点不存在")
        item["custom_domain"] = domain
        _save(items)
        return view(item)


def list_sites():
    with _lock:
        items = _load()
        if not items and os.path.isfile(os.path.join(DEFAULT_DIR, "index.html")):
            items = [{
                "id": "site-" + uuid.uuid4().hex[:8],
                "name": "待办事项网页",
                "path": DEFAULT_DIR,
                "port": 0,
                "visibility": "public",
                "created": time.time(),
                "enabled": True,
                "deploys": [{"time": time.time(), "note": "首次部署"}],
            }]
            _save(items)
        changed = False
        for it in items:
            if not it.get("deploys"):
                it["deploys"] = [{"time": it.get("created") or time.time(),
                                  "note": "首次部署"}]
                changed = True
            if it.get("enabled", True) and it["id"] not in _runtimes:
                try:
                    _start_server(it)
                    changed = True
                except Exception:
                    pass
        if changed:
            _save(items)
        views = [view(it) for it in items]
    # 上次开着公网映射的站点，后端重启后自动重连（映射地址会变，但不需要用户再点一次）
    if cloudflared_path():
        for it, v in zip(items, views):
            if it.get("mapping") and v["status"] == "running" and not v["public_url"]:
                threading.Thread(target=_auto_tunnel, args=(it["id"],), daemon=True).start()
    return views


def _auto_tunnel(sid):
    try:
        tunnel(sid)
    except Exception:
        pass


def deploy(name, path, visibility="public"):
    path = os.path.abspath(str(path or "").strip().strip('"'))
    if not os.path.isdir(path):
        raise SiteError("目录不存在：" + path)
    if not os.path.isfile(os.path.join(path, "index.html")):
        raise SiteError("目录里没有 index.html，无法作为站点根目录")
    with _lock:
        items = _load()
        item = {
            "id": "site-" + uuid.uuid4().hex[:8],
            "name": (name or os.path.basename(path) or "未命名站点")[:40],
            "path": path,
            "port": 0,
            "visibility": visibility if visibility in ("public", "private") else "public",
            "created": time.time(),
            "enabled": True,
            "deploys": [{"time": time.time(), "note": "首次部署"}],
        }
        _start_server(item)
        items.append(item)
        _save(items)
        return view(item)


def redeploy(sid, path=None):
    """对已有站点重新部署：可换目录，保留站点 ID 与访问地址。"""
    with _lock:
        items = _load()
        item = next((it for it in items if it["id"] == sid), None)
        if not item:
            raise SiteError("站点不存在")
        new_path = os.path.abspath(str(path).strip().strip('"')) if path else item["path"]
        if not os.path.isfile(os.path.join(new_path, "index.html")):
            raise SiteError("目录里没有 index.html，无法作为站点根目录")
        if new_path != item.get("path"):
            _stop_server(sid)
            item["path"] = new_path
        item.setdefault("deploys", []).append({"time": time.time(), "note": "重新部署"})
        item["enabled"] = True
        _start_server(item)
        _save(items)
        return view(item)


def set_visibility(sid, visibility):
    with _lock:
        items = _load()
        item = next((it for it in items if it["id"] == sid), None)
        if not item:
            raise SiteError("站点不存在")
        item["visibility"] = "private" if visibility == "private" else "public"
        _save(items)
        return view(item)


def start(sid):
    with _lock:
        items = _load()
        for it in items:
            if it["id"] == sid:
                it["enabled"] = True
                _start_server(it)
                _save(items)
                return view(it)
    raise SiteError("站点不存在")


def stop(sid):
    with _lock:
        items = _load()
        for it in items:
            if it["id"] == sid:
                it["enabled"] = False
                _stop_server(sid)
                _save(items)
                return view(it)
    raise SiteError("站点不存在")


def delete(sid):
    with _lock:
        items = _load()
        rest = [it for it in items if it["id"] != sid]
        if len(rest) == len(items):
            raise SiteError("站点不存在")
        _stop_server(sid)
        _save(rest)
        return True


def cloudflared_path():
    if os.path.isfile(CF_EXE):
        return CF_EXE
    found = shutil.which("cloudflared")
    return found or ""


def download_cloudflared():
    """下载官方 cloudflared（Apache-2.0），供内网穿透使用。"""
    os.makedirs(BIN_DIR, exist_ok=True)
    if os.path.isfile(CF_EXE):
        return CF_EXE
    url = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
           "cloudflared-windows-amd64.exe")
    tmp = CF_EXE + ".part"
    import urllib.request
    with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    if os.path.getsize(tmp) < 5 * 1024 * 1024:
        os.remove(tmp)
        raise SiteError("下载的 cloudflared 体积异常，请稍后重试")
    os.replace(tmp, CF_EXE)
    return CF_EXE


def tunnel(sid):
    """开启公网映射（同一站点重复调用直接复用已有隧道）。"""
    if sid in _pending:
        raise SiteError("映射正在建立中，请稍候")
    _pending.add(sid)
    try:
        return _tunnel_impl(sid)
    except Exception:
        # 映射失败就别保留“已开启”标记，避免每次刷新都重试
        try:
            with _lock:
                items = _load()
                for it in items:
                    if it["id"] == sid:
                        it["mapping"] = False
                _save(items)
        except Exception:
            pass
        raise
    finally:
        _pending.discard(sid)


def _tunnel_impl(sid):
    """给站点开 cloudflared quick tunnel，返回公网地址。"""
    exe = cloudflared_path()
    if not exe:
        raise SiteError("未安装 cloudflared，请先点「安装穿透组件」")
    with _lock:
        items = _load()
        item = next((it for it in items if it["id"] == sid), None)
        if not item:
            raise SiteError("站点不存在")
        _start_server(item)
        if not item.get("mapping"):
            item["mapping"] = True
            _save(items)
        rt = _runtimes.get(sid) or {}
        if rt.get("public_url"):
            return rt["public_url"]
        if rt.get("proc"):
            proc = rt["proc"]
        else:
            port = item["port"]
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            proc = subprocess.Popen(
                [exe, "tunnel", "--url", "http://127.0.0.1:%s" % port,
                 "--no-autoupdate"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                errors="replace", creationflags=flags)
            rt["proc"] = proc
            _runtimes[sid] = rt

    deadline = time.time() + 45
    while time.time() < deadline:
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            if proc.poll() is not None:
                raise SiteError("cloudflared 启动失败（退出码 %s）" % proc.returncode)
            time.sleep(0.2)
            continue
        m = CF_URL_RE.search(line)
        if m:
            rt["public_url"] = m.group(0)
            return rt["public_url"]
    raise SiteError("等待公网地址超时，请检查网络后重试")


def untunnel(sid):
    """关闭站点公网映射，保留本地端口站点。"""
    with _lock:
        items = _load()
        item = next((it for it in items if it["id"] == sid), None)
        if not item:
            raise SiteError("站点不存在")
        item["mapping"] = False
        rt = _runtimes.get(sid) or {}
        proc = rt.get("proc")
        rt["proc"] = None
        rt["public_url"] = ""
        _save(items)
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
    return view(item)

# -*- coding: utf-8 -*-
"""MCP 工具中枢（多 Server 版）

对标 Trae 的 MCP 能力，把 MCP Server 的工具注入 tools.TOOLS 注册表：

- 配置：AI/mcp.json（标准字段，mcpServers 与 servers 两种写法都认）
    {
      "mcpServers": {
        "self":  {"transport": "stdio", "command": "<python>",
                  "args": ["self_mcp_server.py"], "cwd": ".", "namespace": ""},
        "filesystem": {"transport": "stdio", "command": "npx",
                  "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/work"],
                  "namespace": "mcp__fs__"},
        "remote": {"transport": "http", "url": "http://127.0.0.1:9000/mcp",
                  "headers": {"Authorization": "Bearer xxx"}, "namespace": "mcp__remote__"}
      }
    }
- 传输：stdio（子进程）+ http（Streamable HTTP，兼容 JSON / SSE 响应）
- 命名：默认自动加 `mcp__<server>__` 前缀；示例里可显式指定 namespace
  （self 服务器 namespace 为空 → 保持 self_get_config 这类原工具名，向后兼容）
- 能力：懒连接、超时、断线重连一次、工具只读标记、调用审计、状态查询、热重载
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "mcp.json")
PROTOCOL_VERSION = "2024-11-05"
DEFAULT_NAMESPACE = "mcp__{server}__"
CALL_TIMEOUT = 120
# 启动时等待 MCP 就绪的总预算：超时未连上的 server 留待首次调用时懒连接，
# 不能因为它拖慢/阻塞 API 服务启动（uvx 首次装依赖可能耗时数分钟）
STARTUP_JOIN_SEC = 12

# 写类工具判定（MCP 工具若带 annotations.readOnlyHint=true 则视为只读）
_WRITE_HINTS = ("write", "edit", "delete", "remove", "move", "rename", "create",
                "mkdir", "touch", "patch", "upload", "install", "exec", "run",
                "shell", "kill", "restart", "apply", "commit", "push")


# ---------------- 配置 ----------------
def _default_config():
    return {"mcpServers": {
        "self": {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["self_mcp_server.py"],
            "cwd": ".",
            "namespace": "",
            "enabled": True,
        }
    }}


def load_config():
    """读 mcp.json；缺失时按内置 self server 生成一份"""
    if not os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(_default_config(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return _default_config()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except Exception:
        return {}
    servers = cfg.get("mcpServers") or cfg.get("servers") or {}
    return {"mcpServers": servers}


def _fmt_namespace(ns, server):
    if ns is None:
        return DEFAULT_NAMESPACE.format(server=server)
    return str(ns)


def _server_timeout(scfg):
    """每个 server 可单独配 timeout（秒），不写则用全局 CALL_TIMEOUT"""
    try:
        v = float((scfg or {}).get("timeout") or CALL_TIMEOUT)
    except Exception:
        v = CALL_TIMEOUT
    return max(1.0, v)


# ---------------- 传输：stdio ----------------
class StdioClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.timeout = _server_timeout(cfg)
        self.proc = None
        self._id = 0
        self._lock = threading.Lock()
        self._q = queue.Queue()
        self._reader = None

    def alive(self):
        return bool(self.proc and self.proc.poll() is None)

    def connect(self):
        self.close()
        cmd = str(self.cfg.get("command") or "").strip()
        if not cmd:
            raise RuntimeError("缺少 command")
        args = [str(a) for a in (self.cfg.get("args") or [])]
        cwd = self.cfg.get("cwd") or BASE_DIR
        if not os.path.isabs(cwd):
            cwd = os.path.normpath(os.path.join(BASE_DIR, cwd))
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (self.cfg.get("env") or {}).items()})
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        # 用解析后的绝对路径启动（npx / node / python 都能找到）
        exe = shutil.which(cmd) or cmd
        self.proc = subprocess.Popen(
            [exe] + args, cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1, creationflags=flags)
        self._q = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "wasily-agent", "version": "1.0.0"},
        })
        self.notify("notifications/initialized", {})

    def _pump(self):
        proc = self.proc
        try:
            for line in iter(proc.stdout.readline, ""):
                line = line.strip()
                if not line:
                    continue
                try:
                    self._q.put(json.loads(line))
                except Exception:
                    continue
        finally:
            self._q.put(None)      # 结束标记

    def close(self):
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None, timeout=None):
        timeout = timeout or self.timeout
        with self._lock:
            self._id += 1
            rid = self._id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                        "params": params or {}})
            t0 = time.time()
            while time.time() - t0 < timeout:
                try:
                    msg = self._q.get(timeout=max(0.1, timeout - (time.time() - t0)))
                except queue.Empty:
                    raise TimeoutError("MCP server 响应超时")
                if msg is None:
                    raise BrokenPipeError("MCP server 输出流已关闭")
                if msg.get("id") == rid:
                    if "error" in msg:
                        raise RuntimeError(str((msg["error"] or {}).get("message", "MCP 错误")))
                    return msg.get("result", {}) or {}
            raise TimeoutError("MCP server 响应超时")

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})


# ---------------- 传输：Streamable HTTP（JSON / SSE 响应） ----------------
class HttpClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.timeout = _server_timeout(cfg)
        self._id = 0
        self._lock = threading.Lock()
        self._session_id = ""
        self._ready = False

    def alive(self):
        return self._ready

    def connect(self):
        self._ready = False
        self._id = 0
        self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "wasily-agent", "version": "1.0.0"},
        })
        self.notify("notifications/initialized", {})
        self._ready = True

    def close(self):
        self._ready = False

    def _post(self, obj, timeout=None):
        timeout = timeout or self.timeout
        url = str(self.cfg.get("url") or "").strip()
        if not url:
            raise RuntimeError("缺少 url")
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        headers.update({str(k): str(v) for k, v in (self.cfg.get("headers") or {}).items()})
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
            ctype = (resp.headers.get("Content-Type") or "").lower()
            raw = resp.read().decode("utf-8", "replace")
        if "text/event-stream" in ctype or raw.lstrip().startswith("event:") \
                or "\ndata:" in raw:
            return self._parse_sse(raw)
        return json.loads(raw) if raw.strip() else {}

    @staticmethod
    def _parse_sse(raw):
        """从 SSE 文本里取第一条带 result 的 JSON"""
        last = {}
        for block in raw.replace("\r\n", "\n").split("\n\n"):
            data = [ln[5:].strip() for ln in block.split("\n") if ln.startswith("data:")]
            if not data:
                continue
            try:
                obj = json.loads("\n".join(data))
            except Exception:
                continue
            if isinstance(obj, dict):
                if "result" in obj or "error" in obj:
                    return obj
                last = obj
        return last

    def request(self, method, params=None, timeout=None):
        timeout = timeout or self.timeout
        with self._lock:
            self._id += 1
            rid = self._id
            msg = self._post({"jsonrpc": "2.0", "id": rid, "method": method,
                              "params": params or {}}, timeout=timeout)
            if "error" in msg:
                raise RuntimeError(str((msg["error"] or {}).get("message", "MCP 错误")))
            return msg.get("result", {}) or {}

    def notify(self, method, params=None):
        with self._lock:
            self._id += 1
            self._post({"jsonrpc": "2.0", "method": method, "params": params or {}})


# ---------------- Server 状态 ----------------
def _is_write_tool(raw_name, tool):
    ann = tool.get("annotations") or {}
    if isinstance(ann, dict) and ann.get("readOnlyHint") is True:
        return False
    low = str(raw_name).lower()
    return any(h in low for h in _WRITE_HINTS)


class MCPHub:
    def __init__(self):
        self.servers = {}          # name -> state
        self._lock = threading.RLock()

    # ---- 生命周期 ----
    def start(self):
        cfg = load_config()
        # 并发连接：任一 server 首次启动慢（如 uvx 正在装依赖）不能拖住整个后端启动
        threads = []
        for name, scfg in (cfg.get("mcpServers") or {}).items():
            if not isinstance(scfg, dict) or scfg.get("enabled", True) is False:
                continue
            th = threading.Thread(target=self._ensure, args=(name, scfg),
                                  name="mcp-connect-" + name, daemon=True)
            th.start()
            threads.append(th)
        deadline = time.time() + STARTUP_JOIN_SEC
        for th in threads:
            th.join(timeout=max(0.1, deadline - time.time()))
        return self

    def _ensure(self, name, scfg):
        with self._lock:
            if name in self.servers:
                return self.servers[name]
            transport = str(scfg.get("transport") or ("http" if scfg.get("url") else "stdio")).lower()
            client = HttpClient(scfg) if transport in ("http", "sse", "streamable-http") else StdioClient(scfg)
            st = {"name": name, "cfg": scfg, "transport": transport, "client": client,
                  "connected": False, "error": "", "tools": [], "readonly": {}}
            self.servers[name] = st
        try:
            client.connect()
            listed = (client.request("tools/list") or {}).get("tools", []) or []
            st["tools"] = listed
            st["connected"] = True
            for t in listed:
                st["readonly"][t.get("name", "")] = not _is_write_tool(t.get("name", ""), t)
            print(f"[MCP] 服务器 {name} 已连接（{transport}），工具 {len(listed)} 个", flush=True)
        except Exception as e:
            st["error"] = f"{e}"
            print(f"[MCP] 服务器 {name} 连接失败（忽略）: {e}", flush=True)
        return st

    def _client(self, name):
        st = self.servers.get(name)
        if not st:
            raise RuntimeError(f"未配置的 MCP 服务器: {name}")
        client = st["client"]
        if not client.alive():
            client.connect()
            listed = (client.request("tools/list") or {}).get("tools", []) or []
            st["tools"] = listed
            st["connected"] = True
            st["error"] = ""
            for t in listed:
                st["readonly"][t.get("name", "")] = not _is_write_tool(t.get("name", ""), t)
        return st, client

    # ---- 工具注册表 ----
    def tool_registry(self):
        reg = {}
        names = {}                 # 完整工具名 -> (server, raw_name, readonly)
        for name, st in self.servers.items():
            ns = _fmt_namespace(st["cfg"].get("namespace", None), name)
            for t in st["tools"]:
                raw = t.get("name")
                if not raw:
                    continue
                full = f"{ns}{raw}"
                schema = t.get("inputSchema") or {}
                props = schema.get("properties") or {}
                params = {k: (v.get("description") or k) for k, v in props.items()}
                desc = t.get("description") or ""
                desc = f"[MCP:{name}] {desc}".strip()
                readonly = st["readonly"].get(raw, True)

                def _make(srv=name, tool_name=raw):
                    def _fn(**kw):
                        return self.call_tool(srv, tool_name, kw)
                    return _fn

                reg[full] = {"description": desc, "parameters": params,
                             "function": _make(), "_mcp": True}
                names[full] = (name, raw, readonly)
        # 同名保护：MCP 工具不得覆盖项目内置/用户工具（曾导致 kb_search 被顶掉后 KeyError）
        try:
            import tools as _tools
            for full in list(reg.keys()):
                exist = _tools.TOOLS.get(full)
                if isinstance(exist, dict) and not exist.get("_mcp"):
                    reg.pop(full, None)
                    names.pop(full, None)
                    print("[MCP] 工具名 %s 已被内置工具占用，跳过该 MCP 工具" % full,
                          flush=True)
        except Exception:
            pass
        self._names = names
        return reg

    def is_write(self, full_name):
        meta = getattr(self, "_names", {}).get(full_name)
        return False if not meta else not meta[2]

    def tools_prompt(self):
        """生成注入系统提示词的 MCP 段落"""
        lines = []
        for name, st in self.servers.items():
            ns = _fmt_namespace(st["cfg"].get("namespace", None), name)
            state = "已连接" if st["connected"] else ("连接失败: " + st["error"] if st["error"] else "未连接")
            lines.append(f"- 服务器 {name}（{st['transport']}，{state}）工具前缀：{ns or '（无前缀）'}"
                         f"，工具 {len(st['tools'])} 个")
        if not lines:
            return ""
        return ("\n\n## MCP 工具（外部服务器，按需调用）\n"
                + "\n".join(lines) +
                "\n调用方式与普通工具一致（在 ```tool_call 里写完整工具名）。")

    def status(self):
        out = []
        for name, st in self.servers.items():
            out.append({
                "name": name,
                "transport": st["transport"],
                "connected": bool(st["connected"]),
                "error": st["error"],
                "namespace": _fmt_namespace(st["cfg"].get("namespace", None), name),
                "timeout": st["client"].timeout,
                "tools": [{"name": t.get("name"),
                           "description": (t.get("description") or "")[:120],
                           "inputSchema": t.get("inputSchema") or {"properties": {}},
                           "readonly": st["readonly"].get(t.get("name", ""), True)}
                          for t in st["tools"]],
            })
        return out

    def call_tool(self, server, tool, args):
        t0 = time.time()
        try:
            st, client = self._client(server)
            result = client.request("tools/call", {"name": tool, "arguments": args or {}})
            parts = result.get("content", []) or []
            text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")
            text = text or "（工具无输出）"
            if result.get("isError"):
                text = "[MCP 工具错误] " + text
        except Exception as e:
            text = f"[MCP 调用失败] {server}/{tool}: {e}"
        try:
            import audit
            audit.audit_event("mcp_call", f"{server}/{tool} {json.dumps(args, ensure_ascii=False)[:300]}",
                              meta={"ms": int((time.time() - t0) * 1000)})
        except Exception:
            pass
        return text

    def reload(self):
        """重新读配置：断开全部 → 重连 → 返回新工具表"""
        with self._lock:
            for st in self.servers.values():
                try:
                    st["client"].close()
                except Exception:
                    pass
            self.servers = {}
        # 先释放锁再启动：_ensure 线程要拿同一把锁注册 server，
        # 持锁调用 start() 会让重载返回后的短时间内 servers 为空，调用方误报「未配置」。
        self.start()
        deadline = time.time() + 5
        while not self.servers and time.time() < deadline:
            time.sleep(0.05)
        return self.tool_registry()


HUB = MCPHub()


# ==================== IDE 宿主模式 ====================
# 当 IDE 已启动且提供 /mcp/servers 接口时，工具来源切换为 IDE 侧 MCP Host。
# 后端不再自己 spawn MCP 子进程，而是通过 18753 桥调用 IDE 中的 MCP 服务器。

IDE_BRIDGE = "http://127.0.0.1:18753"


def _ide_probe():
    """探测 IDE 桥是否可用（超短超时）"""
    try:
        import urllib.request
        req = urllib.request.Request(IDE_BRIDGE + "/health", method="GET")
        with urllib.request.urlopen(req, timeout=0.8) as r:
            return r.status == 200
    except Exception:
        return False


class IdeProxyHub:
    """IDE 侧 MCP Host 的代理：工具列表和调用都走 18753 桥"""

    def __init__(self):
        self._servers = []
        self._tools = []
        self._names = {}          # full_name -> (server, raw, readonly)
        self._lock = threading.Lock()

    def start(self):
        """从 IDE 拉取工具表"""
        self._refresh()
        print(f"[MCP] IDE Host 模式：{len(self._tools)} 个工具", flush=True)

    def _refresh(self):
        try:
            import urllib.request
            req = urllib.request.Request(IDE_BRIDGE + "/mcp/servers", method="GET")
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read().decode("utf-8") or "{}")
            self._servers = data.get("servers") or []
            req2 = urllib.request.Request(IDE_BRIDGE + "/mcp/tools", method="GET")
            with urllib.request.urlopen(req2, timeout=5) as r2:
                data2 = json.loads(r2.read().decode("utf-8") or "{}")
            self._tools = data2.get("tools") or []
            self._names = {t["name"]: (t.get("server", ""), t.get("raw", ""), t.get("readonly", True))
                           for t in self._tools}
        except Exception as e:
            print(f"[MCP] IDE 工具表拉取失败: {e}", flush=True)
            self._servers = []
            self._tools = []
            self._names = {}

    def tool_registry(self):
        """生成可并入 TOOLS 的注册表"""
        reg = {}
        for t in self._tools:
            full = t["name"]
            props = (t.get("inputSchema") or {}).get("properties") or {}
            params = {k: (v.get("description") or k) for k, v in props.items()}
            desc = (t.get("description") or "").strip()

            def _make(srv=t.get("server", ""), tool_name=t.get("raw", ""), full_name=full):
                def _fn(**kw):
                    return self.call_tool(srv, tool_name, kw, full_name)
                return _fn

            reg[full] = {"description": desc, "parameters": params,
                         "function": _make(), "_mcp": True}
        return reg

    def is_write(self, full_name):
        meta = self._names.get(full_name)
        return False if not meta else not meta[2]

    def tools_prompt(self):
        lines = []
        for s in self._servers:
            state = "已连接" if s.get("connected") else ("连接失败: " + s.get("error", "") if s.get("error") else "未连接")
            ns = s.get("namespace", "")
            lines.append(f"- 服务器 {s['name']}（{s.get('transport','')}，{state}）"
                         f"工具前缀：{ns or '（无前缀）'}，工具 {len(s.get('tools',[]))} 个")
        if not lines:
            return ""
        return ("\n\n## MCP 工具（IDE 托管，按需调用）\n"
                + "\n".join(lines) +
                "\n调用方式与普通工具一致（在 ```tool_call 里写完整工具名）。")

    def status(self):
        return self._servers

    def call_tool(self, server, tool, args, full_name=""):
        """通过 IDE 桥调用 MCP 工具"""
        t0 = time.time()
        if full_name:
            fname = full_name
        else:
            # 从 POST body 来的调用：拼出带前缀的完整工具名
            ns = ""
            for s in self._servers:
                if s.get("name") == server:
                    ns = s.get("namespace", "") or ""
                    break
            fname = ns + tool if ns else tool
        try:
            import urllib.request
            payload = json.dumps({"name": fname, "arguments": args or {}}).encode("utf-8")
            req = urllib.request.Request(IDE_BRIDGE + "/mcp/call", data=payload,
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode("utf-8") or "{}")
            text = data.get("text", "") or "（工具无输出）"
            if not data.get("ok"):
                text = "[MCP 调用失败] " + (data.get("error", "") or text)
        except Exception as e:
            text = f"[MCP 调用失败] {fname}: {e}"
        try:
            import audit
            audit.audit_event("mcp_call_ide", f"{fname} {json.dumps(args, ensure_ascii=False)[:300]}",
                              meta={"ms": int((time.time() - t0) * 1000)})
        except Exception:
            pass
        return text

    def reload(self):
        """兜底模式：只刷新工具表。
        不能再 POST 回 IDE /mcp/reload——新版扩展会把该请求转发回后端，
        两边互相调用会形成死循环。"""
        self._refresh()
        return self.tool_registry()

    def close(self):
        pass  # IDE 侧管理子进程生命周期，后端不干预


# 全局活跃 hub：后端本地中枢是唯一宿主；IDE 托管只在本地连不上时兜底
_active_hub = None


def active():
    """返回当前活跃的 MCP hub"""
    global _active_hub
    if _active_hub is None:
        _active_hub = HUB  # 默认本地
    return _active_hub


def start_and_get_tools():
    """启动 MCP：后端本地中枢优先，全部连不上才退回 IDE 托管。
    反过来（IDE 优先）会让后端和 IDE 扩展互相转发 MCP 请求，重载时直接死循环。"""
    global _active_hub
    try:
        HUB.start()
        _tools = HUB.tool_registry()
        if _tools:
            _active_hub = HUB
            return _tools
        print("[MCP] 本地中枢未连上任何服务器", flush=True)
    except Exception as e:
        print(f"[MCP] 本地中枢启动异常: {e}", flush=True)
    if _ide_probe():
        try:
            proxy = IdeProxyHub()
            proxy.start()
            _tools = proxy.tool_registry()
            if _tools:
                _active_hub = proxy
                return _tools
            print("[MCP] IDE Host 未提供工具", flush=True)
        except Exception as e:
            print(f"[MCP] IDE Host 兜底失败: {e}", flush=True)
    _active_hub = HUB
    return {}


def reload_tools():
    global _active_hub
    try:
        return active().reload()
    except Exception as e:
        print(f"[MCP] 重载失败: {e}", flush=True)
        return {}


def status():
    return active().status()

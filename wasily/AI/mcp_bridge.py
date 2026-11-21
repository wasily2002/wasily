"""
MCP 客户端桥接（api_server 侧）

以 MCP 协议（stdio / JSON-RPC 2.0）连接 self_mcp_server.py，
把它的工具桥接进 tools.TOOLS 注册表（self_* 前缀），
agent 在对话窗口中即可像调用普通工具一样修改自身源文件、重启自身程序。

- 同步调用：一把大锁串行化（与 llama 推理的串行模型一致，够用且稳定）
- 断线自动重连一次（MCP server 崩溃/管道断裂时）
"""
import json
import os
import subprocess
import sys
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_SCRIPT = os.path.join(BASE_DIR, "self_mcp_server.py")


class MCPClient:
    def __init__(self):
        self.proc = None
        self._id = 0
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------
    def connect(self):
        self.close()
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            [sys.executable, SERVER_SCRIPT],
            cwd=BASE_DIR,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1, creationflags=flags)
        self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "wasily-agent", "version": "1.0.0"},
        })
        self._notify("notifications/initialized", {})

    def close(self):
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None

    def alive(self):
        return bool(self.proc and self.proc.poll() is None)

    # ---------- 协议 ----------
    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _read_response(self, req_id, timeout=120):
        """读到匹配 id 的响应为止（跳过通知/其他消息）"""
        import time
        t0 = time.time()
        while time.time() - t0 < timeout:
            line = self.proc.stdout.readline()
            if not line:
                raise BrokenPipeError("MCP server 输出流已关闭")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "MCP 错误"))
                return msg.get("result", {})
        raise TimeoutError("MCP server 响应超时")

    def _request(self, method, params=None):
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id,
                    "method": method, "params": params or {}})
        return self._read_response(self._id)

    def _notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # ---------- 业务 ----------
    def call(self, name, arguments):
        """调用 MCP 工具，返回文本结果；连接断了自动重连重试一次"""
        with self._lock:
            for attempt in (1, 2):
                try:
                    if not self.alive():
                        self.connect()
                    result = self._request("tools/call",
                                           {"name": name, "arguments": arguments})
                    parts = result.get("content", [])
                    text = "\n".join(p.get("text", "") for p in parts
                                     if p.get("type") == "text")
                    if result.get("isError"):
                        return f"[MCP 工具错误] {text}"
                    return text or "（工具无输出）"
                except Exception as e:
                    if attempt == 2:
                        return f"[MCP 连接失败] {e}"
                    try:
                        self.connect()
                    except Exception as e2:
                        return f"[MCP 重连失败] {e2}"

    def list_tools(self):
        with self._lock:
            if not self.alive():
                self.connect()
            return self._request("tools/list").get("tools", [])


_CLIENT = None


def start_and_get_tools():
    """启动 MCP server 并把其工具转换为 TOOLS 注册表条目；
    失败返回空 dict（不影响 agent 其他功能）"""
    global _CLIENT
    try:
        _CLIENT = MCPClient()
        _CLIENT.connect()
        mcp_tools = _CLIENT.list_tools()
    except Exception as e:
        print(f"[MCP] 自我进化服务连接失败（忽略，不影响其他功能）: {e}", flush=True)
        return {}

    registry = {}
    for t in mcp_tools:
        name = t["name"]
        schema = t.get("inputSchema", {}) or {}
        props = schema.get("properties", {}) or {}
        params = {k: (v.get("description") or k) for k, v in props.items()}

        def _make(tool_name):
            def _fn(**kw):
                return _CLIENT.call(tool_name, kw)
            return _fn

        registry[name] = {
            "description": t.get("description", ""),
            "parameters": params,
            "function": _make(name),
        }
    print(f"[MCP] 自我进化工具已接入: {', '.join(registry.keys())}", flush=True)
    return registry

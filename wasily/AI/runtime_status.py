# -*- coding: utf-8 -*-
"""运行中心快照：把后端、桥、站点、隧道、MCP 子进程与桌面进程汇总给 Agent 窗口。

只读采集，不负责启停；任何一项取不到都不影响其它项返回。
"""
import os
import time

import psutil


def _mem(pid):
    try:
        return round(psutil.Process(pid).memory_info().rss / 1048576, 1)
    except Exception:
        return 0


def _started(pid):
    try:
        return time.strftime("%m-%d %H:%M",
                             time.localtime(psutil.Process(pid).create_time()))
    except Exception:
        return ""


def _listen_pids(port):
    pids = set()
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                pids.add(c.pid)
    except Exception:
        pass
    return pids


def _item(name, pid=0, port=0, status="running", detail="", kind=""):
    return {"name": name, "pid": pid or 0, "port": port or 0,
            "status": status, "detail": detail, "kind": kind,
            "mem": _mem(pid) if pid else 0,
            "started": _started(pid) if pid else ""}


def _core_services():
    items = []
    me = os.getpid()
    items.append(_item("后端 API", me, 8000, "running",
                       "api_server.py · 工具与对话中枢", "core"))
    for port, name, kind, desc in ((18753, "IDE 桥", "bridge", "extension.js MCP 代理与指令通道"),
                                   (9000, "远程 MCP", "mcp", "mcp_remote_server.py")):
        pids = _listen_pids(port)
        if pids:
            for pid in sorted(pids):
                items.append(_item(name, pid, port, "running", desc, kind))
        else:
            items.append(_item(name, 0, port, "stopped", "未监听", kind))
    return items


def _site_services():
    """只读站点状态：不能调 sites_net.list_sites()，那个函数会顺带拉起站点、
    还会在端口被占时改端口写回 sites_store.json。"""
    items = []
    try:
        import json
        import sites_net
        with open(sites_net.STORE, encoding="utf-8") as f:
            sites = json.load(f) or []
        runtimes = getattr(sites_net, "_runtimes", {}) or {}
        for s in sites:
            if not isinstance(s, dict):
                continue
            sid = s.get("id")
            rt = runtimes.get(sid) or {}
            running = bool(rt.get("httpd")) and s.get("enabled", True) is not False
            port = s.get("port") or 0
            pids = _listen_pids(port) if running and port else set()
            detail = ("http://127.0.0.1:%s/" % port) if running else "未启动"
            if rt.get("public_url"):
                detail += " · 公网 " + rt["public_url"]
            items.append(_item("站点 · " + (s.get("name") or sid),
                               next(iter(pids), 0), port,
                               "running" if running else "stopped",
                               detail, "site"))
    except Exception:
        pass
    return items


def _proc_name(pid):
    try:
        return psutil.Process(pid).name()
    except Exception:
        return ""


def _tunnels():
    items = []
    ports = {}
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if "cloudflared" not in (p.info["name"] or "").lower():
                continue
            cl = " ".join(p.info["cmdline"] or [])
            port = ""
            if "--url" in cl:
                tail = cl.split("--url", 1)[1].strip().split()[0]
                port = tail.rsplit(":", 1)[-1].strip("/")
            pid = p.info["pid"]
            try:
                ppid = p.ppid()
            except Exception:
                ppid = 0
            parent = _proc_name(ppid) if ppid else ""
            port_n = int(port) if port.isdigit() else 0
            ports[port_n] = ports.get(port_n, 0) + 1
            detail = "cloudflared quick tunnel"
            if parent:
                detail += " · 父进程 " + parent + "(#" + str(ppid) + ")"
            items.append(_item("公网映射隧道", pid, port_n, "running", detail, "tunnel"))
        except Exception:
            continue
    # 同一端口挂多条隧道 = 多次开关映射留下的旧实例，标出来避免误以为是多个服务
    for it in items:
        if ports.get(it["port"], 0) > 1:
            it["detail"] += " · 同端口重复实例"
    return items


def _mcp_children():
    """MCP 工具进程可能挂在后端（本地中枢）也可能挂在 IDE 扩展（IDE Host 代理），
    所以按命令行识别，而不是只看谁的子进程。"""
    items = []
    markers = (
        ("self_mcp_server", "MCP · self"),
        ("mcp_echo_server", "MCP · echo"),
        ("anythingllm_bridge", "MCP · anythingllm"),
        ("server-filesystem", "MCP · filesystem"),
        ("playwright", "MCP · playwright"),
    )
    picked = {}        # 名称 -> (score, item)；同一服务只保留最可信的一个
    # 当前后端自己 spawn 的 MCP 才是活跃连接；IDE 兜底宿主可能有同名旧进程
    own = set()
    try:
        own = {ch.pid for ch in psutil.Process(os.getpid()).children(recursive=True)}
    except Exception:
        pass
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cl = " ".join(p.info["cmdline"] or [])
            low = cl.lower()
            if not cl or "cloudflared" in low:
                continue
            label = ""
            for key, name in markers:
                if key in low:
                    label = name
                    break
            if not label:
                continue
            pid = p.info["pid"]
            try:
                ppid = p.ppid()
            except Exception:
                ppid = 0
            owner = _proc_name(ppid) if ppid else ""
            detail = cl[:150]
            if owner:
                detail += " · 父进程 " + owner + "(#" + str(ppid) + ")"
            it = _item(label, pid, 0, "running", detail, "mcp-proc")
            # 优先保留真正的服务进程（node cli.js / dist/index.js 或 python 脚本），
            # 不要 npx.cmd / cmd.exe 这层包装壳
            score = 0
            norm = low.replace("\\", "/")
            if "npx-cli.js" in norm:
                score = 0            # npx 包装壳，不是真正的服务进程
            elif "dist/index.js" in norm or "cli.js" in norm:
                score = 3
            elif (p.info["name"] or "").lower().startswith("python"):
                score = 2
            elif "npx" not in low:
                score = 1
            prev = picked.get(label)
            if pid in own:
                score += 5
            if prev is None or score > prev[0]:
                picked[label] = (score, it)
        except Exception:
            continue
    items = [v[1] for v in picked.values()]
    return items


def _desktop_processes():
    items = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (p.info["name"] or "").lower()
            cl = " ".join(p.info["cmdline"] or [])
            if name == "electron.exe" and "--agent" in cl:
                items.append(_item("Agent 窗口", p.info["pid"], 0, "running",
                                   "Electron · agent 模式", "desktop"))
            elif "wasily-ide" in cl.lower() and name.endswith(".exe"):
                title = "Wasily IDE"
                items.append(_item(title, p.info["pid"], 0, "running",
                                   "主进程 · 扩展与桥宿主", "desktop"))
        except Exception:
            continue
    # 同一个 IDE 会拉起多个子进程，按标题去重保留内存最大者
    seen = {}
    for it in items:
        key = it["name"]
        if key not in seen or it["mem"] > seen[key]["mem"]:
            seen[key] = it
    return list(seen.values())


def snapshot():
    groups = [
        {"name": "核心服务", "items": _core_services()},
        {"name": "站点服务", "items": _site_services()},
        {"name": "公网映射", "items": _tunnels()},
        {"name": "MCP 子进程", "items": _mcp_children()},
        {"name": "桌面进程", "items": _desktop_processes()},
    ]
    running = sum(1 for g in groups for it in g["items"] if it["status"] == "running")
    total = sum(1 for g in groups for _ in g["items"])
    groups = [g for g in groups if g["items"]]
    return {"ok": True, "now": int(time.time()),
            "totals": {"running": running, "total": total}, "groups": groups}

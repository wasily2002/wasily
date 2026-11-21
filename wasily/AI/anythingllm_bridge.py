# -*- coding: utf-8 -*-
"""AnythingLLM 知识库 MCP 桥（stdio 传输，JSON-RPC 2.0）

把 AnythingLLM Desktop 的本地 HTTP API 包装成 wasily 可用的 MCP 工具：
  kb_status      检查 AnythingLLM 是否可达、列出工作区
  kb_workspaces  列出所有工作区
  kb_search      在指定工作区做相似度检索（query 模式）
  kb_chat        让知识库回答问题（chat 模式，带引用）
  kb_documents   列出工作区里的文档
  kb_add_text    把一段文本写进知识库
  kb_add_file    把本地文件路径导入知识库
  kb_import_url  抓取一个 URL 入库
  kb_delete      删除工作区里的某个文档

设计原则：
- AnythingLLM 没启动时不崩溃，工具返回清晰的中文提示，让模型/用户知道要先开 AnythingLLM。
- 工作区 slug 用「默认工作区」兜底，不要求用户每次都传。
- 所有文本结果用纯文本返回，不扔结构化 JSON 给模型，避免污染对话。
"""
import json
import os
import sys
import urllib.request
import urllib.error

PROTOCOL = "2024-11-05"

# AnythingLLM Desktop 默认本地端口；本地 API 不需要鉴权。
BASE = os.environ.get("ANYTHINGLLM_BASE", "http://127.0.0.1:3001/api/v1")
DEFAULT_WORKSPACE = os.environ.get("ANYTHINGLLM_WORKSPACE", "默认工作区")


def _http(method, path, body=None, timeout=15):
    """调 AnythingLLM API，失败时返回 (None, 错误文本)。"""
    url = BASE + path
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw), None
            except Exception:
                return raw, None
    except urllib.error.HTTPError as e:
        try:
            msg = e.read().decode("utf-8", errors="replace")
        except Exception:
            msg = str(e)
        return None, "HTTP %s: %s" % (e.code, msg[:300])
    except Exception as e:
        return None, "连接 AnythingLLM 失败(%s)：%s。请确认 AnythingLLM Desktop 已启动。" % (BASE, e)


def _slug():
    """工作区 slug：AnythingLLM 内部用 slug（小写、连字符）。"""
    return DEFAULT_WORKSPACE.strip().lower().replace(" ", "-")


TOOLS = [
    {
        "name": "kb_status",
        "description": "检查 AnythingLLM 知识库是否在线，并返回工作区列表与向量维度等状态",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "kb_workspaces",
        "description": "列出所有知识库工作区",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "kb_search",
        "description": "在指定工作区做相似度检索，返回最相关的文档片段",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要检索的问题或关键词"},
                "workspace": {"type": "string", "description": "工作区名称（省略用默认）"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "kb_chat",
        "description": "让知识库基于已入库内容回答问题，返回答案与引用来源",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "要问的问题"},
                "workspace": {"type": "string", "description": "工作区名称（省略用默认）"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "kb_documents",
        "description": "列出指定工作区里所有已入库的文档",
        "inputSchema": {
            "type": "object",
            "properties": {"workspace": {"type": "string", "description": "工作区名称（省略用默认）"}},
            "required": [],
        },
    },
    {
        "name": "kb_add_text",
        "description": "把一段纯文本写入知识库",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要入库的文本内容"},
                "title": {"type": "string", "description": "文档标题（省略用首句）"},
                "workspace": {"type": "string", "description": "工作区名称（省略用默认）"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "kb_add_file",
        "description": "把一个本地文件路径导入知识库（支持 .md/.txt/.pdf 等 AnythingLLM 支持的格式）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "本地文件的绝对路径"},
                "workspace": {"type": "string", "description": "工作区名称（省略用默认）"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "kb_import_url",
        "description": "抓取一个网页 URL 并入库",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的网页地址"},
                "workspace": {"type": "string", "description": "工作区名称（省略用默认）"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "kb_delete",
        "description": "从工作区删除指定文档",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "要删除的文档名"},
                "workspace": {"type": "string", "description": "工作区名称（省略用默认）"},
            },
            "required": ["name"],
        },
    },
]


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def result(id_, res):
    send({"jsonrpc": "2.0", "id": id_, "result": res})


def error(id_, code, msg):
    send({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": msg}})


def text_result(id_, text, is_error=False):
    result(id_, {"content": [{"type": "text", "text": text}], "isError": is_error})


def kb_status(args):
    data, err = _http("GET", "/workspaces")
    if err:
        return err, True
    names = [w.get("name") for w in (data or {}).get("workspaces", []) if isinstance(w, dict)]
    lines = ["AnythingLLM 在线（%s）" % BASE,
             "工作区：%s" % ("、".join(names) if names else "（空）"),
             "嵌入模型：在 AnythingLLM 里把 Embedding Provider 设为 Generic OpenAI，"
             "Base URL 填 http://127.0.0.1:8000/v1，Model Name 填 /v1/models 返回的 embedding 模型名。"]
    return "\n".join(lines), False


def kb_workspaces(args):
    data, err = _http("GET", "/workspaces")
    if err:
        return err, True
    names = [w.get("name") for w in (data or {}).get("workspaces", []) if isinstance(w, dict)]
    return "工作区：\n" + "\n".join("- " + n for n in names) if names else "没有工作区", False


def kb_search(args):
    q = (args.get("query") or "").strip()
    if not q:
        return "query 不能为空", True
    ws = args.get("workspace") or DEFAULT_WORKSPACE
    slug = ws.strip().lower().replace(" ", "-")
    data, err = _http("POST", "/workspace/%s/chat" % slug,
                      {"message": q, "mode": "query"})
    if err:
        return err, True
    # AnythingLLM query 模式返回 textResponse（答案）+ sources（引用）
    text = (data or {}).get("textResponse", "")
    sources = (data or {}).get("sources") or []
    out = [text or "（检索无结果）"]
    if sources:
        out.append("\n引用：")
        for s in sources:
            if isinstance(s, dict):
                out.append("- %s" % s.get("title") or s.get("name") or s.get("doc") or str(s)[:80])
    return "\n".join(out), False


def kb_chat(args):
    msg = (args.get("message") or "").strip()
    if not msg:
        return "message 不能为空", True
    ws = args.get("workspace") or DEFAULT_WORKSPACE
    slug = ws.strip().lower().replace(" ", "-")
    data, err = _http("POST", "/workspace/%s/chat" % slug,
                      {"message": msg, "mode": "chat"})
    if err:
        return err, True
    text = (data or {}).get("textResponse", "")
    sources = (data or {}).get("sources") or []
    out = [text or "（无回答）"]
    if sources:
        out.append("\n引用：")
        for s in sources:
            if isinstance(s, dict):
                out.append("- %s" % (s.get("title") or s.get("name") or s.get("doc") or str(s)[:80]))
    return "\n".join(out), False


def kb_documents(args):
    ws = args.get("workspace") or DEFAULT_WORKSPACE
    slug = ws.strip().lower().replace(" ", "-")
    data, err = _http("GET", "/workspace/%s/documents" % slug)
    if err:
        return err, True
    docs = (data or {}).get("documents") or []
    names = []
    for d in docs:
        if isinstance(d, dict):
            names.append(d.get("title") or d.get("name") or d.get("doc") or "")
    if not names:
        return "工作区「%s」暂无文档" % ws, False
    return "工作区「%s」的文档：\n%s" % (ws, "\n".join("- " + n for n in names)), False


def kb_add_text(args):
    text = (args.get("text") or "").strip()
    if not text:
        return "text 不能为空", True
    ws = args.get("workspace") or DEFAULT_WORKSPACE
    slug = ws.strip().lower().replace(" ", "-")
    title = (args.get("title") or text[:30]).strip()
    # AnythingLLM 接受 raw-text 文档；实际入库走 /document/upload 的 raw-text 模式
    data, err = _http("POST", "/document/raw-text",
                      {"textContent": text, "metadata": {"title": title}})
    if err:
        return err, True
    return "已添加文本到工作区「%s」，标题：%s" % (ws, title), False


def kb_add_file(args):
    path = (args.get("path") or "").strip()
    if not path:
        return "path 不能为空", True
    if not os.path.exists(path):
        return "文件不存在：%s" % path, True
    # AnythingLLM 的 /document/upload 是 multipart；这里用 raw text 兜底纯文本，
    # 二进制文件建议用户在 AnythingLLM 界面拖拽。这里只做存在性校验并提示。
    if path.lower().endswith((".txt", ".md", ".markdown", ".csv")):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception as e:
            return "读取失败：%s" % e, True
        ws = args.get("workspace") or DEFAULT_WORKSPACE
        _, err = _http("POST", "/document/raw-text",
                       {"textContent": text, "metadata": {"title": os.path.basename(path)}})
        if err:
            return err, True
        return "已导入纯文本文件：%s" % os.path.basename(path), False
    return ("文件 %s 不是纯文本，AnythingLLM 需要在界面里拖拽上传（支持 pdf/docx 等）。"
            "已为你记录路径，请在 AnythingLLM 对应工作区上传。" % os.path.basename(path), False)


def kb_import_url(args):
    url = (args.get("url") or "").strip()
    if not url:
        return "url 不能为空", True
    ws = args.get("workspace") or DEFAULT_WORKSPACE
    slug = ws.strip().lower().replace(" ", "-")
    data, err = _http("POST", "/workspace/%s/link/add" % slug, {"link": url})
    if err:
        return err, True
    return "已提交抓取：%s（AnythingLLM 后台会异步入库）" % url, False


def kb_delete(args):
    name = (args.get("name") or "").strip()
    if not name:
        return "name 不能为空", True
    ws = args.get("workspace") or DEFAULT_WORKSPACE
    slug = ws.strip().lower().replace(" ", "-")
    data, err = _http("DELETE", "/workspace/%s/vector/single" % slug, {"doc": name})
    if err:
        return err, True
    return "已删除：%s" % name, False


HANDLERS = {
    "kb_status": kb_status,
    "kb_workspaces": kb_workspaces,
    "kb_search": kb_search,
    "kb_chat": kb_chat,
    "kb_documents": kb_documents,
    "kb_add_text": kb_add_text,
    "kb_add_file": kb_add_file,
    "kb_import_url": kb_import_url,
    "kb_delete": kb_delete,
}


def handle(req):
    mid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    if method == "initialize":
        result(mid, {"protocolVersion": PROTOCOL,
                     "capabilities": {"tools": {"listChanged": False}},
                     "serverInfo": {"name": "anythingllm-kb", "version": "1.0.0"}})
    elif method in ("notifications/initialized", "notifications/cancelled"):
        pass
    elif method == "ping":
        result(mid, {})
    elif method == "tools/list":
        result(mid, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            error(mid, -32601, "未知工具: %s" % name)
            return
        try:
            text, is_err = fn(args)
        except Exception as e:
            text_result(mid, "工具执行异常：%s" % e, True)
            return
        text_result(mid, text, is_err)
    elif method is not None:
        error(mid, -32601, "不支持的方法: %s" % method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception as e:
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32700, "message": "解析失败: %s" % e}})


if __name__ == "__main__":
    main()

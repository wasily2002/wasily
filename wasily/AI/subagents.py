"""子智能体（Subagent）机制：Markdown 定义 + 独立上下文 + 工具白名单 + 防递归。

目录优先级（后者覆盖同名前者）：
    AI/agents/*.md                 内置
    %USERPROFILE%/.wasily/agents   用户级
    {项目根}/.wasily/agents        项目级

文件格式：
    ---
    name: search
    description: 什么场景下该调用它
    tools: file_search, file_read
    disallowedTools: shell_exec
    max_rounds: 6
    ---
    系统提示词正文

运行期上下文用 threading.local 保存，作用域仅限当前子智能体调用，
因此不会串到其他会话或主循环。
"""
import os
import re
import threading
import time

_TLS = threading.local()
_CACHE = {"at": 0.0, "map": {}}
_CACHE_TTL = 2.0

# 判定子智能体是否需要写权限（决定它跑在 ask 还是 solo 模式）
WRITE_TOOLS = {
    "file_write", "save_output", "file_delete", "file_move", "file_rename",
    "shell_exec", "shell_admin", "process_start", "process_kill",
    "apply_patch", "run_python", "run_code", "doc_write", "video_render",
    "ide_edit_file", "ide_run_terminal",
}


# ---------------- 运行期上下文 ----------------

def current_ctx():
    return getattr(_TLS, "ctx", None)


def set_ctx(ctx):
    _TLS.ctx = ctx


def current_spec():
    ctx = current_ctx()
    return (ctx or {}).get("spec")


# ---------------- 路径与解析 ----------------

def _builtin_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents")


def _user_dir():
    return os.path.join(os.path.expanduser("~"), ".wasily", "agents")


def _repo_dir():
    """代码仓库根（AI 目录向上两层）。项目根 .wasily/agents 放在这里也能被发现，
    不依赖 IDE 当前打开的工作区。"""
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, ".wasily", "agents")


def _project_dir():
    try:
        import tools as _tools
        root = _tools.get_workspace_root()
    except Exception:
        root = os.getcwd()
    return os.path.join(root or os.getcwd(), ".wasily", "agents")


def _split_list(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v or "").strip()
    if not s:
        return []
    return [p.strip() for p in s.replace("，", ",").split(",") if p.strip()]


def _parse(text):
    """解析 frontmatter（不依赖 PyYAML）：支持 key: value 与 key:\\n  - item。"""
    meta, body = {}, text
    m = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if m:
        body = text[m.end():]
        cur = None
        for raw in m.group(1).splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue
            stripped = line.strip()
            if stripped.startswith("- ") and cur:
                meta.setdefault(cur, [])
                if isinstance(meta[cur], list):
                    meta[cur].append(stripped[2:].strip().strip('"').strip("'"))
                continue
            if ":" in stripped:
                k, v = stripped.split(":", 1)
                k, v = k.strip(), v.strip()
                if v == "":
                    meta[k] = []
                    cur = k
                else:
                    meta[k] = v.strip('"').strip("'")
                    cur = None
    return meta, body


def _load_file(path, level):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except Exception:
        return None
    meta, body = _parse(raw)
    name = str(meta.get("name") or os.path.splitext(os.path.basename(path))[0]).strip()
    if not name:
        return None
    try:
        rounds = int(meta.get("max_rounds") or 6)
    except Exception:
        rounds = 6
    rounds = max(1, min(rounds, 12))
    _raw_tools = meta.get("tools")
    tools = _split_list(_raw_tools)
    return {
        "name": name,
        "description": str(meta.get("description") or "").strip(),
        "model": str(meta.get("model") or "").strip(),
        # None = 未写 tools（不限制）；[] = 写了 tools 但为空（零工具，必须显式区分）
        "tools": (tools if tools else ([] if _raw_tools is not None else None)),
        "disallowed": _split_list(meta.get("disallowedTools")),
        "max_rounds": rounds,
        "prompt": body.strip(),
        "path": path,
        "level": level,
    }


def load_agents(force=False):
    now = time.time()
    if not force and _CACHE["map"] and (now - _CACHE["at"]) < _CACHE_TTL:
        return _CACHE["map"]
    result = {}
    for level, d in (("builtin", _builtin_dir()), ("user", _user_dir()),
                     ("repo", _repo_dir()), ("project", _project_dir())):
        try:
            files = sorted(os.listdir(d))
        except Exception:
            continue
        for fn in files:
            if not fn.lower().endswith(".md"):
                continue
            spec = _load_file(os.path.join(d, fn), level)
            if spec:
                result[spec["name"]] = spec     # 后扫描的层级覆盖同名
    _CACHE["map"] = result
    _CACHE["at"] = now
    return result


def get_agent(name):
    key = str(name or "").strip()
    if not key:
        return None
    agents = load_agents()
    if key in agents:
        return agents[key]
    low = key.lower()
    for k, v in agents.items():
        if k.lower() == low:
            return v
    return None


def list_agents():
    out = []
    for spec in load_agents().values():
        out.append({
            "name": spec["name"],
            "description": spec["description"],
            "level": spec["level"],
            "tools": spec["tools"] or [],
            "max_rounds": spec["max_rounds"],
            "path": spec["path"],
        })
    return sorted(out, key=lambda x: x["name"])


# ---------------- 工具权限 ----------------

def tool_allowed(spec, tool_name):
    """子智能体是否允许调用某工具。spec 为 None 表示不在子智能体上下文（不限）。"""
    if not spec:
        return True
    name = str(tool_name or "")
    if name == "agent_delegate":       # 防递归：任何层级都不允许再委派
        return False
    if name in (spec.get("disallowed") or []):
        return False
    allowed = spec.get("tools")
    if allowed is not None:            # [] 表示显式零工具，不能用 truthy 判断
        return name in allowed
    return True


def check_call(spec, tool_name):
    """执行前校验：允许返回空串，拒绝返回给模型的错误文案。"""
    if not spec:
        return ""
    if tool_name == "agent_delegate":
        return "❌ 子智能体不允许再委派子智能体（防递归，max_depth=1）。"
    if not tool_allowed(spec, tool_name):
        return ("❌ 工具 %s 不在子智能体「%s」的权限范围内。请改用可用工具完成任务。"
                % (tool_name, spec.get("name")))
    return ""


def tool_list(spec):
    """子智能体可见的工具名清单（仅用于 system prompt 展示）。"""
    try:
        import tools as _tools
    except Exception:
        return list(spec.get("tools") or [])
    if spec.get("tools") is not None:
        return [n for n in spec["tools"] if n in _tools.TOOLS]
    try:
        names = _tools.enabled_tool_names()
    except Exception:
        names = list(_tools.TOOLS.keys())
    return [n for n in names if tool_allowed(spec, n)]


def exec_mode(spec):
    """只读子智能体跑 ask 模式；声明了写类工具则跑 solo。"""
    names = set(tool_list(spec))
    return "solo" if names & WRITE_TOOLS else "ask"


def build_system_prompt(spec):
    lines = [
        "你是 Wasily 的子智能体「%s」。" % spec["name"],
        "你由主智能体委派执行一个边界明确的任务，拥有独立上下文；主对话历史你不可见。",
        "",
        spec.get("prompt") or "",
        "",
        "可用工具（仅限以下，调用其他工具会被直接拒绝）：",
    ]
    names = tool_list(spec)
    for n in names:
        lines.append("- " + n)
    if not names:
        lines.append("- （无）")
    lines += [
        "",
        "约束：",
        "1. 只完成委派给你的任务，不要扩展范围，不要提出后续开发计划。",
        "2. 不能调用 agent_delegate 再委派子智能体。",
        "3. 完成后直接输出结果正文，不要调用 task_done。",
        "4. 找不到就明确写「未找到」，禁止编造路径、数字或代码。",
        "5. 输出要紧凑：只保留结论和必要依据（路径/行号/片段）。",
    ]
    return "\n".join(lines)


def agents_prompt():
    """主循环 system prompt 用的可委派清单；子智能体运行期间返回空（不列自身）。"""
    if current_ctx():
        return ""
    agents = list_agents()
    if not agents:
        return ""
    lines = ["可委派的子智能体（调用后拥有独立上下文，只返回结论）："]
    for a in agents:
        desc = a["description"] or "（未写 description）"
        lines.append("- %s: %s" % (a["name"], desc))
    lines.append("")
    lines.append('调用方式：```tool_call\n{"name": "agent_delegate", "parameters": {"name": "子智能体名", "task": "要它做的具体事情（它看不到当前对话，必须写全背景）"}}\n```')
    lines.append("适用：需要大量检索/分析、但最终只要一个结论的任务。一次性读几个文件直接用 file_read，不必委派。")
    return "\n".join(lines)

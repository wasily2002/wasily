# -*- coding: utf-8 -*-
"""任务编排器（Trae 式）：听懂用户意图 → 拆角色人物 → 拆任务模块 → 路由 agent/skill → 备齐工具。

核心约定
- 人物 people：2~5 位不同职责的专家，每个任务只归属一个人物
- 任务 tasks：按序排列、可声明前置依赖；系统自动补齐用户容易忽略的「环境准备 / 验证测试 / 交付总结」
- agent：内置角色库（每个角色有自己的方法论提示词与拿手工具）
- skill：skills/*.md，命中后把技能指引注入该任务；缺失时按角色方法论兜底
- tools：任务声明的工具全部校验；已注册直接复用，缺依赖自动 pip 安装并回报状态

模型输出约定（```tasks 代码块）：
```tasks
{"goal":"一句话目标",
 "people":[{"agent":"planner","name":"规划师","duty":"把需求拆清楚"}],
 "tasks":[{"title":"调研现状","person":"planner","kind":"需求拆解","detail":"...",
           "skill":"","tools":["file_read"],"depends":[]}]}
```
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

# ============ 内置角色库（agent）============
AGENTS = {
    "planner": {
        "name": "规划师", "emoji": "🧭",
        "duty": "听懂真实意图、拆解目标与验收标准、排任务顺序",
        "tools": ["file_list", "file_read", "memory_save"],
        "prompt": "先确认目标与约束（谁用、什么场景、验收标准），再拆解步骤，指出风险与前置依赖；"
                  "不写实现代码，只交付清晰可执行的方案。",
    },
    "researcher": {
        "name": "调研员", "emoji": "🔎",
        "duty": "查资料/翻代码/比方案，给出事实依据",
        "tools": ["file_search", "file_read", "network_request", "code_search"],
        "prompt": "先找证据再下结论：列出查到的关键事实、可行的 2~3 个方案及取舍，明确推荐哪一个和理由，"
                  "不要凭想象编造数据。",
    },
    "designer": {
        "name": "设计师", "emoji": "🎨",
        "duty": "界面结构、视觉风格、交互流程",
        "tools": ["file_write", "screenshot"],
        "prompt": "先定信息层级与主色调，再落地为具体布局/组件/响应式断点；输出可直接实现的 HTML/CSS 或设计说明，"
                  "避免空泛的形容词。",
    },
    "coder": {
        "name": "工程师", "emoji": "💻",
        "duty": "写代码、改代码、重构与联调",
        "tools": ["file_read", "file_write", "file_list", "run_python", "shell_exec", "self_edit", "self_write"],
        "prompt": "先读相关源码再动手；改动保持最小且完整可运行，写完自己核对语法与引用路径；"
                  "不要留下 TODO 或半成品。",
    },
    "builder": {
        "name": "构建师", "emoji": "🏗️",
        "duty": "安装依赖、构建打包、启动服务与进程",
        "tools": ["shell_exec", "process_start", "process_list", "network_ping"],
        "prompt": "先检查运行环境与依赖是否齐备，再做构建/启动；把关键命令与输出摘要汇报，失败时给出回退方案。",
    },
    "tester": {
        "name": "测试员", "emoji": "🧪",
        "duty": "按验收标准验证功能，找出缺陷",
        "tools": ["run_python", "shell_exec", "file_read", "network_request", "screenshot"],
        "prompt": "围绕验收标准逐条验证，记录实际结果与预期差异；能复现的缺陷给出复现步骤和建议修复点。",
    },
    "operator": {
        "name": "系统管家", "emoji": "🖥️",
        "duty": "本机环境、文件整理、进程窗口与系统配置",
        "tools": ["shell_exec", "file_list", "process_list", "service_list", "system_info", "env_get"],
        "prompt": "操作本机前先确认影响范围；批量文件/系统级改动先说明后果，做完汇报实际变化。",
    },
    "writer": {
        "name": "撰稿人", "emoji": "✍️",
        "duty": "文档/报告/说明/交付物整理",
        "tools": ["file_write", "save_output", "file_read"],
        "prompt": "结论先行、分点清晰、有数据或文件佐证；交付物保存到产物库或指定目录，并在回复里给出路径。",
    },
    "reviewer": {
        "name": "校对员", "emoji": "🔍",
        "duty": "检查错别字、语病与内容一致性",
        "tools": ["file_read"],
        "prompt": "逐条列出发现的问题与改法，不重写用户内容；没有问题时明确说没有问题。",
    },
}

# 任务类型 → 默认角色（模型给的 person 缺失或无法识别时兜底）
KIND_AGENT = [
    ("需求", "planner"), ("规划", "planner"), ("调研", "researcher"), ("分析", "researcher"),
    ("设计", "designer"), ("界面", "designer"), ("样式", "designer"),
    ("代码", "coder"), ("实现", "coder"), ("开发", "coder"), ("重构", "coder"), ("修复", "coder"),
    ("构建", "builder"), ("部署", "builder"), ("环境", "builder"), ("依赖", "builder"),
    ("验证", "tester"), ("测试", "tester"), ("验收", "tester"),
    ("系统", "operator"), ("文件", "operator"), ("清理", "operator"), ("配置", "operator"),
    ("文档", "writer"), ("报告", "writer"), ("交付", "writer"), ("总结", "writer"), ("文案", "writer"),
    ("校对", "reviewer"), ("审阅", "reviewer"),
]

# 工具别名（模型常写错，映射到真实注册名）
ALIASES = {
    "read_file": "file_read", "write_file": "file_write", "list_files": "file_list",
    "list_dir": "file_list", "run_shell": "shell_exec", "run_command": "shell_exec",
    "run_terminal": "shell_exec", "execute": "shell_exec", "search_file": "file_search",
    "find_file": "file_search", "http_request": "network_request", "fetch": "network_request",
    "download": "network_download", "open_url": "open_app", "open_app": "open_app",
    "screenshot": "screenshot", "python": "run_python", "run_code": "run_python",
}

# 缺失能力 → 需要安装的 pip 包（供 run_python/shell_exec 使用）
INSTALL_MAP = {
    "pdf": "pypdf", "pdf_read": "pypdf", "pdf_parse": "pypdf",
    "excel": "openpyxl", "xlsx": "openpyxl", "word": "python-docx", "docx": "python-docx",
    "ppt": "python-pptx", "pptx": "python-pptx", "image": "pillow", "pil": "pillow",
    "ocr": "pytesseract", "browser": "playwright", "selenium": "selenium",
    "http": "requests", "requests": "requests", "html": "beautifulsoup4",
    "bs4": "beautifulsoup4", "crawl": "beautifulsoup4", "dataframe": "pandas",
    "numpy": "numpy", "chart": "matplotlib", "plot": "matplotlib", "qrcode": "qrcode",
    "jieba": "jieba", "yaml": "pyyaml", "pinyin": "pypinyin", "email": "yagmail",
    "websocket": "websockets", "psutil": "psutil", "markdown": "markdown",
}

_install_lock = threading.Lock()
_INSTALL_CACHE = {}          # pip 包 -> (ok, message, ts)


def _skill_module():
    try:
        import skills as m
        return m
    except Exception:
        return None


def get_agent(key):
    """按 id 或中文名解析角色；解析不到返回 None"""
    if not key:
        return None
    k = str(key).strip()
    if k in AGENTS:
        return k, AGENTS[k]
    for aid, a in AGENTS.items():
        if a["name"] == k or a["name"] in k or k in a["name"]:
            return aid, a
    return None


def guess_agent(kind="", title="", detail=""):
    text = f"{kind} {title} {detail}"
    for kw, aid in KIND_AGENT:
        if kw in text:
            return aid
    return "coder"


def section(simple=False, action=True):
    """注入系统提示词：任务编排规则 + 角色库 + 技能库 + 工具清单；
    simple=True 时完全不提供编排格式（7B 模型只要看到格式模板就会照着输出，导致简单请求只吐卡片）；
    action=False 表示本轮是纯问答/解释，不能再写「必须调用真实工具」，否则与 BASE 的「纯闲聊直接回答」冲突，
   7B 会反复产出工具调用而正文为空。"""
    if simple:
        guide = ("本轮只是一个简单的单步请求：直接调用真实工具执行，然后把真实结果用简洁中文回答用户。"
                 if action else
                 "本轮是纯问答/解释请求：直接用简洁中文回答，不要调用任何工具。")
        return "\n".join([
            "",
            "## 本轮执行方式（最高优先级，压过其它任何说明）",
            guide,
            "【严禁】输出 ```tasks 块或 ```plan 块，也【严禁】拆成多角色分工——那会让用户干等审批而拿不到结果。",
            "",
        ])
    lines = [
        "",
        "## 任务编排（复杂多步任务）",
        "3 步以上 / 多文件 / 多角色 / 需求含糊时，先输出 tasks 块（必须是回复第一段，块后最多两句话），"
        "然后停下等用户确认；简单问答与单步操作不要输出。",
        "```tasks",
        '{"goal":"目标≤40字","tasks":[{"title":"步骤","person":"planner","kind":"拆解",'
        '"detail":"做什么/涉及文件","skill":"","tools":["file_read"],"depends":[]}],'
        '"people":[{"agent":"planner","name":"规划师","duty":"拆需求"}]}',
        "```",
        "规则：",
        "- tasks 写在 people 前；每任务只归一位人物；person 用 people 中 id/角色名；agent 只能选角色库。",
        "- skill 只填技能库命中名（否则空字符串）；tools 只填工具清单真实名，0~5 个；"
        "tasks 3~10 个，depends 填前置序号。",
        "- 补齐环境/依赖、验证验收、交付总结；goal≤40字，duty≤20字，detail≤40字，JSON 不换行。",
        "",
        "### 角色库（agent）",
    ]
    for aid, a in AGENTS.items():
        lines.append(f"- {aid}｜{a['name']}：{a['duty']}；工具：{', '.join(a['tools'])}")
    lines.append("")
    lines.append("### 技能库（skill）")
    m = _skill_module()
    sk = m.load_skills() if m else []
    if sk:
        for s in sk:
            lines.append(f"- {s['name']}（触发词：{'、'.join(s['triggers'][:3])}）")
    else:
        lines.append("- （暂无技能，skill 留空）")
    # 工具清单不再重复列出：BASE_PROMPT 已包含完整工具名与参数，重复一份会让 prompt 白白膨胀约 1000 字
    return "\n".join(lines)


def catalog():
    """给前端/调试用的能力清单"""
    m = _skill_module()
    sk = m.load_skills() if m else []
    try:
        from tools import TOOLS
        tool_names = [n for n in TOOLS.keys() if not n.startswith("_")]
    except Exception:
        tool_names = []
    return {
        "agents": [{"id": k, "name": v["name"], "emoji": v["emoji"],
                    "duty": v["duty"], "tools": v["tools"]} for k, v in AGENTS.items()],
        "skills": [{"name": s["name"], "triggers": s["triggers"],
                    "description": s["description"]} for s in sk],
        "tools": tool_names,
    }


# ============ 解析模型输出的任务编排块 ============

def _scan_json_objects(raw):
    """从可能被截断的 JSON 文本里逐个抠出完整对象（跳过字符串内的括号，支持任意嵌套）"""
    objs, stack, in_str, esc = [], [], False, False
    for i, ch in enumerate(raw):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}":
            if stack:
                start = stack.pop()
                try:
                    objs.append(json.loads(raw[start:i + 1]))
                except Exception:
                    pass
    return objs


def _repair_truncated(raw):
    """模型输出被 max_tokens 截断时的兜底：拼回 goal + people + tasks"""
    objs = _scan_json_objects(raw)
    tasks = [o for o in objs if isinstance(o, dict) and o.get("title")]
    if not tasks:
        tasks = [o for o in objs if isinstance(o, dict) and o.get("name")
                 and ("person" in o or "kind" in o)]
    people = [o for o in objs if isinstance(o, dict) and o.get("agent")
              and not o.get("title") and not o.get("person")]
    if not tasks:
        return None
    goal = ""
    mg = re.search(r'"goal"\s*:\s*"([^"]*)"', raw)
    if mg:
        goal = mg.group(1)
    if not goal:
        goal = str(tasks[0].get("title") or "")
    return {"goal": goal, "people": people, "tasks": tasks}


def parse_tasks_block(text):
    """解析 ```tasks 代码块，结构合法返回 dict，否则 None（容忍 JSON 被截断）"""
    if not text:
        return None
    m = re.search(r"```tasks\s*\n?(.*?)(?:\n?```|$)", text, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    raw = m.group(1).strip()
    data = None
    try:
        data = json.loads(raw)
    except Exception:
        mm = re.search(r"\{[\s\S]*\}", raw)
        if mm:
            try:
                data = json.loads(mm.group(0))
            except Exception:
                data = None
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        data = _repair_truncated(raw)
    if not isinstance(data, dict):
        return None
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return None
    return data


def strip_tasks_block(text):
    return re.sub(r"```tasks\s*\n?[\s\S]*?(?:\n?```|$)", "", text or "",
                  flags=re.DOTALL | re.IGNORECASE).strip()


def _norm_people(raw_people, tasks):
    people, seen = [], set()
    if isinstance(raw_people, list):
        for p in raw_people:
            if isinstance(p, str):
                p = {"agent": p}
            if not isinstance(p, dict):
                continue
            hit = get_agent(p.get("agent") or p.get("name") or p.get("person"))
            if not hit:
                continue
            aid, a = hit
            if aid in seen:
                continue
            seen.add(aid)
            people.append({"agent": aid,
                           "name": str(p.get("name") or a["name"])[:20],
                           "emoji": a["emoji"],
                           "duty": str(p.get("duty") or a["duty"])[:60]})
    # 任务里出现但 people 没声明的角色，自动补进人物列表
    for t in tasks:
        hit = get_agent(t.get("person") or t.get("agent"))
        if hit and hit[0] not in seen:
            aid, a = hit
            seen.add(aid)
            people.append({"agent": aid, "name": a["name"], "emoji": a["emoji"], "duty": a["duty"]})
    if not people:
        aid = guess_agent(str(tasks[0].get("kind", "")), str(tasks[0].get("title", "")))
        a = AGENTS[aid]
        people.append({"agent": aid, "name": a["name"], "emoji": a["emoji"], "duty": a["duty"]})
    return people


def _norm_tools(raw):
    out = []
    if isinstance(raw, str):
        raw = [x for x in re.split(r"[,\s、，]+", raw) if x]
    if not isinstance(raw, list):
        return out
    for t in raw:
        n = str(t).strip()
        if not n:
            continue
        n = ALIASES.get(n.lower(), n)
        if n not in out:
            out.append(n)
    return out[:6]


def _norm_skill(raw):
    s = str(raw or "").strip()
    if not s or s.lower() in ("none", "null", "无", "空", "-"):
        return ""
    return s[:30]


def _norm_tasks(raw_tasks):
    tasks = []
    for t in raw_tasks[:10]:
        if isinstance(t, str):
            t = {"title": t}
        if not isinstance(t, dict):
            continue
        title = str(t.get("title") or t.get("name") or "").strip()
        if not title:
            continue
        kind = str(t.get("kind") or "").strip()[:12]
        person = t.get("person") or t.get("agent") or ""
        hit = get_agent(person)
        aid = hit[0] if hit else guess_agent(kind, title, str(t.get("detail", "")))
        deps = t.get("depends") or []
        if isinstance(deps, (int, str)):
            deps = [deps]
        deps = [int(d) for d in deps if str(d).isdigit()] if isinstance(deps, list) else []
        tasks.append({
            "title": title[:60],
            "kind": kind or "任务",
            "detail": str(t.get("detail") or "")[:300],
            "agent": aid,
            "skill": _norm_skill(t.get("skill")),
            "tools": _norm_tools(t.get("tools")),
            "depends": deps,
            "status": "pending",
            "result": "",
            "tools_state": [],
        })
    return tasks


def _tool_registered(name):
    try:
        from tools import TOOLS
        return name in TOOLS
    except Exception:
        return False


def _needs_install(tasks):
    """任务里声明的工具，是否出现「未注册但可安装」的依赖"""
    for t in tasks:
        for n in t.get("tools", []):
            if not _tool_registered(n) and n.lower() in INSTALL_MAP:
                return True
    return False


def auto_complete(tasks):
    """补齐用户容易漏掉的环节：环境与依赖准备 / 验证与验收 / 交付与总结"""
    has_verify = any(re.search(r"验证|测试|验收|检查", t["title"] + t["kind"]) for t in tasks)
    has_deliver = any(re.search(r"交付|总结|汇总|汇报|文档|报告", t["title"] + t["kind"]) for t in tasks)

    if _needs_install(tasks) and not re.search(r"环境|依赖|安装", " ".join(t["title"] for t in tasks[:2])):
        tasks.insert(0, {
            "title": "环境与依赖准备", "kind": "环境准备",
            "detail": "检查运行环境，安装任务所需依赖，确认工具可用",
            "agent": "builder", "skill": "", "tools": ["shell_exec"], "depends": [],
            "status": "pending", "result": "", "tools_state": [],
        })
        # 环境准备插到最前，原有任务序号顺延，依赖序号同步 +1
        for t in tasks[1:]:
            t["depends"] = [d + 1 for d in (t.get("depends") or [])]

    if not has_verify:
        last = len(tasks)
        tasks.append({
            "title": "验证与验收", "kind": "验证测试",
            "detail": "按验收标准逐条验证产物，记录实际结果与遗留问题",
            "agent": "tester", "skill": "", "tools": ["run_python", "file_read"], "depends": [last],
            "status": "pending", "result": "", "tools_state": [],
        })

    if not has_deliver:
        last = len(tasks)
        tasks.append({
            "title": "交付与总结", "kind": "交付",
            "detail": "汇总产物路径、使用方法与后续建议，保存到产物库",
            "agent": "writer", "skill": "", "tools": ["save_output"], "depends": [last],
            "status": "pending", "result": "", "tools_state": [],
        })

    for i, t in enumerate(tasks):
        t["id"] = i + 1
    return tasks[:12]


def build_plan(data):
    """把模型的任务编排块规范化为 {goal, people, steps}"""
    raw_tasks = [t for t in data.get("tasks", []) if isinstance(t, (dict, str))]
    tasks = _norm_tasks(raw_tasks)
    if not tasks:
        return None
    people = _norm_people(data.get("people"), tasks)
    tasks = auto_complete(tasks)
    # 补齐 auto_complete 新加角色到人物列表
    known = {p["agent"] for p in people}
    for t in tasks:
        if t["agent"] not in known:
            a = AGENTS.get(t["agent"], AGENTS["coder"])
            known.add(t["agent"])
            people.append({"agent": t["agent"], "name": a["name"],
                           "emoji": a["emoji"], "duty": a["duty"]})
    for t in tasks:
        a = AGENTS.get(t["agent"], AGENTS["coder"])
        t["agent_name"] = a["name"]
        t["emoji"] = a["emoji"]
    return {"goal": str(data.get("goal") or tasks[0]["title"])[:200],
            "people": people, "steps": tasks}


def match_skill(text):
    """按技能触发词匹配技能名；匹配不到返回空串"""
    t = (text or "").lower()
    if not t:
        return ""
    m = _skill_module()
    if not m:
        return ""
    best, best_hit = "", 0
    for s in m.load_skills():
        hit = sum(1 for kw in s["triggers"] if kw and kw.lower() in t)
        if hit > best_hit:
            best, best_hit = s["name"], hit
    return best


def from_legacy_plan(plan_obj):
    """兼容旧的 ```plan 计划块，转换成任务编排结构（自动补角色/技能/工具）"""
    steps = []
    for s in plan_obj.get("steps", []):
        title = s.get("title", "")
        detail = s.get("detail", "")
        aid = guess_agent(s.get("kind", ""), title, detail)
        a = AGENTS[aid]
        steps.append({"title": title, "kind": s.get("kind", "任务"),
                      "detail": detail, "agent": aid, "agent_name": a["name"],
                      "emoji": a["emoji"],
                      "skill": match_skill(f"{title} {detail}"),
                      "tools": a["tools"][:3],
                      "depends": [], "status": "pending", "result": "", "tools_state": []})
    if not steps:
        return None
    steps = auto_complete(steps)
    for s in steps:
        a = AGENTS.get(s.get("agent"), AGENTS["coder"])
        s["agent_name"] = a["name"]
        s["emoji"] = a["emoji"]
    people, known = [], set()
    for s in steps:
        if s["agent"] not in known:
            known.add(s["agent"])
            people.append({"agent": s["agent"], "name": s["agent_name"],
                           "emoji": s["emoji"], "duty": AGENTS[s["agent"]]["duty"]})
    return {"goal": plan_obj.get("goal", ""), "people": people, "steps": steps}


# ============ 工具准备（缺的自动安装）============

def _pip_install(pkg, timeout=300):
    """安装单个 pip 包：先默认源，失败回退清华镜像；结果缓存"""
    with _install_lock:
        hit = _INSTALL_CACHE.get(pkg)
        if hit:
            return hit
        cmds = [[sys.executable, "-m", "pip", "install", "-q", pkg],
                [sys.executable, "-m", "pip", "install", "-q", pkg,
                 "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"]]
        msg = "安装失败"
        for cmd in cmds:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=timeout, encoding="utf-8", errors="ignore")
                if r.returncode == 0:
                    msg = f"已安装 {pkg}"
                    res = (True, msg)
                    _INSTALL_CACHE[pkg] = res
                    return res
                msg = (r.stderr or r.stdout or "").strip()[-160:]
            except Exception as e:
                msg = str(e)[:160]
        res = (False, msg)
        _INSTALL_CACHE[pkg] = res
        return res


def ensure_tools(names, allow=True, emit=None, installer=None):
    """校验/准备任务所需工具，返回 [{tool, status, detail}]
    status: ready 已注册 | installed 已自动安装依赖 | missing 无法提供
    installer(pkg) -> (ok, msg)：由调用方决定安装策略（白名单/确认），不传则直接 pip 安装"""
    out = []
    for name in _norm_tools(names):
        if _tool_registered(name):
            out.append({"tool": name, "status": "ready", "detail": "已就绪"})
            continue
        pkg = INSTALL_MAP.get(name.lower())
        if not pkg:
            out.append({"tool": name, "status": "missing",
                        "detail": "未注册且无自动安装方案，改用现有工具实现"})
            continue
        if not allow:
            out.append({"tool": name, "status": "missing", "detail": "自动安装已关闭"})
            continue
        if emit:
            try:
                emit("tool_install", tool=name, pkg=pkg, state="start")
            except Exception:
                pass
        ok, msg = installer(pkg) if installer else _pip_install(pkg)
        out.append({"tool": name, "status": "installed" if ok else "missing",
                    "detail": msg})
        if emit:
            try:
                emit("tool_install", tool=name, pkg=pkg,
                     state="done" if ok else "failed", detail=msg)
            except Exception:
                pass
    return out


def skill_prompt(name):
    """按技能名取技能指引；找不到时返回空（由角色方法论兜底）"""
    if not name:
        return ""
    m = _skill_module()
    if not m:
        return ""
    for s in m.load_skills():
        if s["name"] == name or name in s["name"] or s["name"] in name:
            return f"### 适用技能：{s['name']}\n{s['prompt']}"
    return ""


def step_prompt(step, tools_state=None):
    """为单个任务生成注入提示词：角色身份 + 方法论 + 技能 + 工具就绪情况"""
    aid = step.get("agent") or "coder"
    a = AGENTS.get(aid, AGENTS["coder"])
    lines = [
        f"你现在是【{a['name']} {a['emoji']}】，只负责本步骤，不要越界做其他步骤的事。",
        f"你的职责：{a['duty']}",
        f"工作方法：{a['prompt']}",
    ]
    sk = skill_prompt(step.get("skill"))
    if sk:
        lines.append(sk)
    st = tools_state if tools_state is not None else step.get("tools_state") or []
    ready = [x["tool"] for x in st if x["status"] == "ready"]
    installed = [x["tool"] for x in st if x["status"] == "installed"]
    bad = [f"{x['tool']}（{x['detail']}）" for x in st if x["status"] == "missing"]
    if ready:
        lines.append("本步可用工具（可直接调用）：" + ", ".join(ready))
    if installed:
        lines.append("已自动装好的依赖库：" + ", ".join(installed)
                     + "（不是工具名，请用 run_python/shell_exec 调用它们）")
    if bad:
        lines.append("以下工具不可用，请用现有工具或 run_python 变通实现：" + "；".join(bad))
    dep = step.get("depends") or []
    if dep:
        lines.append("前置步骤已完成：第 " + "、".join(str(d) for d in dep) + " 步。")
    lines.append("")
    lines.append(f"本步任务（{step.get('kind', '任务')}）：{step.get('title', '')}")
    if step.get("detail"):
        lines.append(f"要求：{step['detail']}")
    lines.append("做完后用一两句话汇报本步产出（做了什么、产物路径、结果如何），不要复述其他步骤。")
    return "\n".join(lines)

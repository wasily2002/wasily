"""多模态任务路由：文档 / 代码 / 系统操作 / 网页 / 数据。

目标是不让用户手动挑工具：先按意图归类，再匹配已有 skill，
并把执行前置条件（依赖、权限、工作目录）在动手前告诉用户。
"""
import importlib.util
import os
import re
import shutil
import sys

import config as cfg
import skills as skills_mod

DOC = re.compile(
    r"word|docx|文档|报告|公文|合同|协议|ppt|幻灯片|演示|excel|xlsx|表格|"
    r"pdf|简历|方案书|可研|标书|markdown|\.md\b", re.I)
CODE = re.compile(
    r"代码|函数|类|接口|bug|报错|异常|调试|重构|编译|运行|脚本|程序|"
    r"项目|模块|依赖|单元测试|\.(py|js|ts|tsx|java|go|rs|cpp|c|vue|html|css)\b", re.I)
OS = re.compile(
    r"打开|安装|卸载|进程|服务|桌面|窗口|文件夹|目录|系统设置|注册表|环境变量|"
    r"终端|命令行|cmd|powershell|批量重命名|清理|截图|录屏", re.I)
WEB = re.compile(
    r"网页|网站|浏览器|爬虫|抓取|前端|页面|部署站点|落地页|交互效果", re.I)
DATA = re.compile(
    r"数据|统计|分析|汇总|图表|可视化|csv|json|清洗|报表", re.I)

# 分类 → 优先 skill 名称（与 skills/*.md 的 name 对齐）
SKILL_PRIORITY = {
    "doc": ["文档编写", "公文写作", "报告生成"],
    "code": ["代码编辑", "Python开发"],
    "os": ["系统服务", "IDE集成"],
    "web": ["网页开发", "unslop-ui", "浏览器操作"],
    "data": ["Python开发"],
}

AGENT_MAP = {"doc": "wasily", "code": "coder", "os": "ops",
             "web": "coder", "data": "coder"}


def classify(text):
    t = str(text or "")
    scores = {
        "doc": len(DOC.findall(t)),
        "code": len(CODE.findall(t)),
        "os": len(OS.findall(t)),
        "web": len(WEB.findall(t)),
        "data": len(DATA.findall(t)),
    }
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "chat"
    # 文档里出现「代码/函数」时，代码意图优先，避免把「写代码文档」误判成文档生成
    if scores["code"] and re.search(r"代码|函数|bug|调试|报错", t, re.I) and scores["doc"] <= scores["code"]:
        return "code"
    return best


def _has_module(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def preflight(category):
    """返回 [{name, ok, required, hint}]，ok=False 表示需要用户先处理。"""
    rows = []
    if category == "doc":
        for mod, pkg, label in (("docx", "python-docx", "Word 写入"),
                                ("openpyxl", "openpyxl", "Excel 读写"),
                                ("pptx", "python-pptx", "PPT 生成")):
            ok = _has_module(mod)
            rows.append({"name": label, "ok": ok, "required": False,
                         "hint": "" if ok else "缺少 %s，可让 AI 安装（pip install %s）" % (pkg, pkg)})
    elif category == "code":
        rows.append({"name": "Python", "ok": bool(sys.executable), "required": True,
                     "hint": ""})
        node = shutil.which("node")
        rows.append({"name": "Node.js", "ok": bool(node), "required": False,
                     "hint": "" if node else "涉及前端/JS 项目时需要，可安装 Node.js"})
        root = ""
        try:
            import tools as tools_mod
            root = tools_mod.get_workspace_root() if hasattr(tools_mod, "get_workspace_root") else ""
        except Exception:  # noqa: BLE001
            root = os.getcwd()
        rows.append({"name": "可写工作目录", "ok": bool(root), "required": True,
                     "hint": "" if root else "请先打开一个项目文件夹"})
    elif category == "os":
        psutil_ok = _has_module("psutil")
        rows.append({"name": "系统信息读取 (psutil)", "ok": psutil_ok, "required": False,
                     "hint": "" if psutil_ok else "缺少 psutil，进程/资源类操作会受限"})
        rows.append({"name": "工具执行权限", "ok": True, "required": True,
                     "hint": "受保护模式下高危命令会先弹确认，可在输入框左下角切换为完全访问"})
    elif category == "web":
        node = shutil.which("node")
        rows.append({"name": "本地预览（Node/Python）", "ok": bool(node) or bool(sys.executable),
                     "required": False, "hint": ""})
        rows.append({"name": "浏览器自动化", "ok": shutil.which("npx") is not None,
                     "required": False,
                     "hint": "" if shutil.which("npx") else "需要 Node.js/npx 才能调用 Playwright"})
    elif category == "data":
        rows.append({"name": "Python 运行环境", "ok": bool(sys.executable), "required": True,
                     "hint": ""})
        rows.append({"name": "pandas（可选）", "ok": _has_module("pandas"), "required": False,
                     "hint": "" if _has_module("pandas") else "未安装时改用标准库处理，能力略受限"})
    return rows


def pick_skill(category, matched=None):
    matched = matched or []
    names = {str(s.get("name") or "") for s in matched}
    for want in SKILL_PRIORITY.get(category, []):
        for s in matched:
            if s.get("name") == want:
                return s
    # 触发词没命中时，从技能目录按名字补一个
    try:
        catalog = {s["name"]: s for s in skills_mod.load_skills()}
    except Exception:  # noqa: BLE001
        catalog = {}
    for want in SKILL_PRIORITY.get(category, []):
        if want in catalog:
            return catalog[want]
    return None


def route(text, matched=None):
    if not bool(cfg.get("task_router.auto", True)):
        return None
    category = classify(text)
    if category == "chat":
        return None
    skill = pick_skill(category, matched)
    checks = preflight(category)
    blockers = [c for c in checks if c.get("required") and not c.get("ok")]
    return {
        "category": category,
        "skill": (skill or {}).get("name") or "",
        "agent": AGENT_MAP.get(category, ""),
        "checks": checks,
        "blockers": blockers,
        "needs_confirm": bool(blockers),
    }


def prompt_section(info):
    """把路由结果拼进系统提示词，约束模型自动按选定技能推进。"""
    if not info:
        return ""
    label = {"doc": "文档编辑", "code": "代码编写与调试", "os": "本地系统操作",
             "web": "网页开发", "data": "数据处理"}.get(info["category"], info["category"])
    lines = ["", "## 自动任务路由（系统已判定，直接执行，不要反问用户用哪个工具）",
             "- 场景：%s" % label]
    if info.get("skill"):
        lines.append("- 已自动匹配技能：%s（按其步骤执行）" % info["skill"])
    if info.get("agent"):
        lines.append("- 以【%s】智能体身份执行，不要要求用户手动切换。" % info["agent"])
    ready = [c["name"] for c in info["checks"] if c["ok"]]
    if ready:
        lines.append("- 环境就绪：" + "、".join(ready))
    warn = [c for c in info["checks"] if not c["ok"]]
    if warn:
        lines.append("- 执行前需告知用户的前置条件：")
        for c in warn:
            lines.append("  · %s：%s" % (c["name"], c["hint"] or "不可用"))
    lines.append("- 需要用户确认或补充信息时，一次性列全，不要分多轮挤牙膏式追问。")
    return "\n".join(lines)

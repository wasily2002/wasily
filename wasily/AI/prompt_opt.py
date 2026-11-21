"""输入优化（对话框 ✨）。

默认 basic：纯规则、毫秒级返回，不占模型、不阻塞输入。
高级模式 llm：调用本地模型改写，可在设置里切换并自定义系统提示词。
off：保留原文，仅返回提示。
"""
import json
import re
import urllib.request

import config as cfg

BASIC_SYSTEM = (
    "你是资深提示词工程师。把用户草稿改写成可直接执行的高质量中文提示词："
    "明确角色、目标、上下文、输出格式与约束。只输出提示词本身。"
)

_TASK_HINTS = {
    "doc": "输出为结构化文档，先给大纲再展开，术语准确、可直接交付。",
    "code": "先定位相关文件与函数，再给改动方案；改动需可编译/可运行，并说明验证步骤。",
    "os": "涉及本机操作时说明影响范围；高风险步骤先列出并等待确认。",
    "web": "先确认视觉与交互目标，再给完整实现；保证响应式与可访问性。",
    "data": "先说明数据来源与口径，再给处理步骤和结果校验方式。",
}


def _clean(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _suggest(context):
    """输入框为空时，根据对话上下文给一句可继续的提问。"""
    t = _clean(context)
    if not t:
        return "帮我梳理当前项目结构，并给出下一步最值得做的三件事。"
    if re.search(r"报错|失败|异常|不能|无法", t):
        return "根据上面的报错，定位根因并给出最小修复方案，改完后帮我验证。"
    if re.search(r"文档|报告|方案", t):
        return "把上面的内容整理成一份结构清晰、可直接交付的文档。"
    if re.search(r"代码|函数|接口", t):
        return "继续完善上面的实现，补齐边界处理和验证步骤。"
    return "基于上面的内容继续深入，给出可执行结论和下一步行动。"


def basic_optimize(text, context="", template=""):
    draft = _clean(text)
    if not draft:
        return _suggest(context)
    try:
        import task_router
        category = task_router.classify(draft)
    except Exception:  # noqa: BLE001
        category = "chat"
    hint = _TASK_HINTS.get(category, "结论先行，给出可执行的下一步。")
    # 保留用户原意，不替换关键词；只补结构与约束
    lines = [
        "【任务】" + draft,
        "【背景】" + (_clean(context)[-300:] if context else "由当前对话上下文决定。"),
        "【执行要求】",
        "1. 先判断信息是否充分，不足时一次性列出需要补充的内容，不要反复追问。",
        "2. " + hint,
        "3. 结论先行，过程简洁；涉及事实时标注来源或依据。",
        "【输出格式】",
        "- 先给结论/结果，再给必要说明；命令、路径、代码保持可直接复制。",
        "- 有多个方案时给出推荐项及理由。",
    ]
    if template:
        lines.append("【补充约束】" + _clean(template)[:300])
    return "\n".join(lines)


def _llm_optimize(text, context="", template=""):
    sys_prompt = _clean(template) or BASIC_SYSTEM
    user = (("对话背景：\n" + _clean(context)[-1200:] + "\n\n") if context else "") + \
        "我的草稿：" + _clean(text)
    body = json.dumps({
        "messages": [{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": user}],
        "max_tokens": 700, "temperature": 0.4, "raw": True,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:8000/v1/chat/completions",
        data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    out = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", out.strip()).strip()
    return out


def optimize(text, context="", mode=None):
    """返回 {ok, mode, text, fallback, error}。任何异常都退回 basic，保证不阻塞输入。"""
    chosen = str(mode or cfg.get("prompt_optimize.mode", "basic") or "basic").lower()
    if chosen == "off":
        return {"ok": True, "mode": "off", "text": _clean(text),
                "fallback": False, "error": ""}
    template = str(cfg.get("prompt_optimize.template", "") or "")
    if chosen == "llm" and _clean(text):
        try:
            out = _llm_optimize(text, context, template)
            if out:
                return {"ok": True, "mode": "llm", "text": out,
                        "fallback": False, "error": ""}
        except Exception as exc:  # noqa: BLE001 —— 模型不可用时不能卡住输入
            out = basic_optimize(text, context, template)
            return {"ok": True, "mode": "llm", "text": out, "fallback": True,
                    "error": str(exc)[:160]}
    max_chars = int(cfg.get("prompt_optimize.max_chars", 1200) or 1200)
    out = basic_optimize(text, context, template)[:max_chars]
    return {"ok": True, "mode": "basic", "text": out, "fallback": False, "error": ""}

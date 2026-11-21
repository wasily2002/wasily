"""时效性公司查询的自动联网检索。

模型自带的静态知识无法保证公司信息（融资、股价、人事、产品、新闻）的时效性，
所以这里在生成之前先检索，把结果作为高优先级上下文注入系统提示词，
并要求模型只依据检索结果回答、在末尾列出可溯源链接。

检索失败不阻断对话：返回 ok=False，由调用方退回普通回答并提示「未能联网」。
"""
import importlib.util
import json
import os
import re
import threading
import time

import config as cfg

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_WEB_TOOL = os.path.join(BASE_DIR, "user_tools", "web_search.py")

_CACHE = {}
_CACHE_LOCK = threading.Lock()
_MOD = {"m": None}

# 公司/企业主体特征词
_COMPANY = re.compile(
    r"公司|企业|集团|品牌|厂商|创业公司|独角兽|上市公司|官网|创始[人者]|董事长|CEO|"
    r"法人|融资|估值|营收|财报|股价|市值|总部|招聘|供应商|收购|并购|发布会|产品线|"
    r"工商|注册地|联系方式|经营状况|市场份额|竞争对手",
    re.I,
)
# 时效性特征词
_FRESH = re.compile(
    r"最新|最近|近期|今天|昨日|今年|目前|当前|实时|新闻|动态|进展|消息|刚刚|"
    r"20\d{2}\s*年|一季度|二季度|三季度|四季度|上半年|下半年",
    re.I,
)
# 显式检索意图
_EXPLICIT = re.compile(
    r"查一下|查查|搜一下|搜索一下|搜索|联网|查询|帮我查|找一下|了解一下|"
    r"网上|资料|信息",
    re.I,
)
# 代码/本地操作类请求不触发联网
_CODEISH = re.compile(
    r"```|\.(py|js|ts|tsx|vue|java|go|rs|html|css|json|md)\b|"
    r"改代码|修\s*bug|调试|编译|函数|接口|数据库|本地文件|这个文件|当前项目",
    re.I,
)


def _load_tool():
    if _MOD["m"] is not None:
        return _MOD["m"]
    spec = importlib.util.spec_from_file_location("_wasily_web_search", _WEB_TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _MOD["m"] = mod
    return mod


def should_search(text):
    """判断是否需要在生成前联网检索。"""
    if not bool(cfg.get("web.auto_search", True)):
        return False
    t = str(text or "").strip()
    if not t or len(t) > 300:
        return False
    if _CODEISH.search(t):
        return False
    company = bool(_COMPANY.search(t))
    fresh = bool(_FRESH.search(t))
    explicit = bool(_EXPLICIT.search(t))
    if company and (fresh or explicit):
        return True
    # 「查一下 XX 公司」这类明确检索意图
    if explicit and re.search(r"公司|企业|集团|品牌", t):
        return True
    # 明确要求联网查某个主体（未必带「公司」二字，如「帮我查一下特斯拉」），
    # 但本地文件/项目类请求不联网。
    if explicit and len(t) <= 40 and not re.search(
            r"本地|电脑|文件|项目|目录|代码|配置|日志|进程", t):
        return True
    return False


def extract_query(text):
    """从口语请求里抽一个适合搜索引擎的短查询。"""
    q = str(text or "").strip()
    q = re.sub(r"^[，,。.\s]*(请|麻烦|帮我|你能|可以|能不能|帮我查|我想知道)+", "", q)
    q = re.sub(r"^(查一下|查查|搜一下|搜索一下|搜索|查询|联网查|找一下|了解一下|看看)[:：,，\s]*",
               "", q)
    # 截掉「怎么样/是什么」这类问句尾巴，保留主体
    q = re.split(r"(的最新|最近|近期|今天|目前|现在|的新闻|的动态|的官网|的创始人|"
                 r"的融资|的财报|的股价|的市值|怎么样|是什么|有哪些|吗[？?]?$)", q)[0]
    q = q.strip("，,。.！!？? \t\n")
    if len(q) < 2:
        q = str(text or "").strip()[:60]
    return q[:80]


def search(query, count=None, engine=None):
    """调用现有 web_search 工具做结构化检索；返回 (results, error)。"""
    mod = _load_tool()
    n = int(count or cfg.get("web.count", 6) or 6)
    eng = str(engine or cfg.get("web.engine", "bing") or "bing")
    try:
        if hasattr(mod, "search_results"):
            rows = mod.search_results(query, engine=eng, count=n)
            if rows:
                return rows, ""
            if eng != "baidu":
                rows = mod.search_results(query, engine="baidu", count=n)
                return rows, "" if rows else "无检索结果"
            return [], "无检索结果"
        text = mod.run(query=query, engine=eng, count=n)
        return [], text[:400]
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)


def _cache_get(key):
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if item and time.time() - item[0] < int(cfg.get("web.cache_ttl", 600) or 600):
            return item[1]
        if item:
            _CACHE.pop(key, None)
    return None


def _cache_set(key, value):
    with _CACHE_LOCK:
        if len(_CACHE) > 64:
            _CACHE.clear()
        _CACHE[key] = (time.time(), value)


def build_context(text, timeout=None):
    """检索并返回 {ok, query, results, text, error}。"""
    query = extract_query(text)
    cached = _cache_get(query)
    if cached:
        return cached
    limit = float(timeout or cfg.get("web.timeout", 10) or 10)
    box = {"rows": None, "err": ""}

    def _work():
        try:
            box["rows"], box["err"] = search(query)
        except Exception as exc:  # noqa: BLE001
            box["err"] = str(exc)

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(limit)
    if th.is_alive():
        result = {"ok": False, "query": query, "results": [],
                  "error": "检索超时", "text": ""}
        _cache_set(query, result)
        return result
    rows = box["rows"] or []
    if not rows:
        result = {"ok": False, "query": query, "results": [],
                  "error": box["err"] or "未检索到结果", "text": ""}
        _cache_set(query, result)
        return result
    lines = ["## 实时联网检索结果（生成回答时的最高优先级事实来源）",
             "检索时间：%s　查询：%s　来源数量：%d" % (
                 time.strftime("%Y-%m-%d %H:%M"), query, len(rows))]
    for i, row in enumerate(rows, 1):
        lines.append("%d. %s" % (i, row.get("title") or "（无标题）"))
        lines.append("   链接：%s" % (row.get("url") or ""))
        if row.get("snippet"):
            lines.append("   摘要：%s" % str(row["snippet"])[:300])
    lines.append(
        "\n硬性要求：\n"
        "1) 涉及该公司的时效性信息（融资、股价、人事、产品、新闻、联系方式等）只能以上述检索结果为准，"
        "禁止使用模型记忆或过时静态知识作答。\n"
        "2) 检索结果没有覆盖的信息，明确写「未检索到可靠公开信息」，不得编造。\n"
        "3) 回答末尾用「来源：标题（链接）」列出实际引用的条目。"
    )
    result = {"ok": True, "query": query, "results": rows,
              "error": "", "text": "\n".join(lines)}
    _cache_set(query, result)
    return result


def prompt_section(ctx):
    if not ctx or not ctx.get("ok"):
        return ""
    return "\n\n" + str(ctx.get("text") or "")

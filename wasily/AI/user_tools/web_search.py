# -*- coding: utf-8 -*-
"""
联网搜索工具：多关键词检索，返回标题+链接+摘要。
写合同/协议/可研报告时，AI 先调用本工具扫描互联网参考资料，
再用 fetch_web 抓取重点页面全文，最后生成文档。
对标 Trae 的「搜索 N 个关键词，参考 N 篇资料」能力。
"""

import json
import re
import urllib.parse
import urllib.request

SPEC = {
    "name": "web_search",
    "description": (
        "联网搜索：输入关键词，返回搜索引擎结果（标题+链接+摘要），供 AI 参考多篇资料。"
        "写合同/协议/可研报告/公文时，先用本工具检索相关政策、范本、条款，"
        "再用 fetch_web 抓取重点页面全文，最后生成有依据的文档。"
        "支持一次传多个关键词（用 | 分隔），并行检索。"
    ),
    "parameters": {
        "query": "搜索关键词。多个关键词用 | 分隔，如「邮政业务代办 协议 保证金|邮政服务商 权益保障」",
        "engine": "搜索引擎：bing(默认，稳定) / baidu(中文结果多)",
        "count": "每个关键词返回结果数，默认 8，最多 20",
    },
}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _fetch(url, timeout=12):
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        # Bing 默认 utf-8；百度可能 gbk，尝试解码
        raw = r.read()
        ct = r.headers.get("Content-Type", "") or ""
        if "gbk" in ct.lower() or "gb2312" in ct.lower():
            return raw.decode("gbk", errors="ignore")
        return raw.decode("utf-8", errors="ignore")


def _strip_tags(html):
    return re.sub(r"<[^>]+>", "", html or "").strip()


def _bing(query, count):
    url = "https://www.bing.com/search?q=" + urllib.parse.quote(query) + \
          f"&count={min(count, 20)}&setlang=zh-CN&cc=CN"
    html = _fetch(url)
    results = []
    # Bing 结果块：<li class="b_algo">...<h2><a href="...">标题</a></h2>...<p>摘要</p>
    for m in re.finditer(
            r'<li class="b_algo"[^>]*>.*?<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?</h2>(.*?)</li>',
            html, re.DOTALL):
        link = m.group(1)
        title = _strip_tags(m.group(2))
        body = m.group(3)
        # 摘要：<p class="b_lineclamp...">...</p> 或 <p>...</p>
        sm = re.search(r'<p[^>]*>(.*?)</p>', body, re.DOTALL)
        snippet = _strip_tags(sm.group(1)) if sm else ""
        if title and link.startswith("http"):
            results.append({"title": title, "url": link, "snippet": snippet[:300]})
        if len(results) >= count:
            break
    return results


def _baidu(query, count):
    url = "https://www.baidu.com/s?wd=" + urllib.parse.quote(query) + \
          f"&rn={min(count, 20)}"
    html = _fetch(url)
    results = []
    # 百度结果块：<h3 class="..."><a href="...">标题</a></h3>...摘要
    for m in re.finditer(
            r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?</h3>',
            html, re.DOTALL):
        link = m.group(1)
        title = _strip_tags(m.group(2))
        if title and link:
            results.append({"title": title, "url": link, "snippet": ""})
        if len(results) >= count:
            break
    return results


def _search_one(query, engine, count):
    try:
        if engine == "baidu":
            return _baidu(query, count)
        return _bing(query, count)
    except Exception as e:
        return {"error": str(e)}


def search_results(query, engine="bing", count=8):
    """结构化检索入口：供 web_context 等内部模块复用，返回 [{title,url,snippet}]。"""
    query = (query or "").strip()
    if not query:
        return []
    count = max(1, min(int(count or 8), 20))
    res = _search_one(query, (engine or "bing").strip().lower(), count)
    if isinstance(res, dict):
        return []
    return res


def run(query="", engine="bing", count=8):
    query = (query or "").strip()
    if not query:
        return "请提供搜索关键词。"
    engine = (engine or "bing").strip().lower()
    count = max(1, min(int(count or 8), 20))
    # 多关键词：| 分隔，并行检索
    queries = [q.strip() for q in query.split("|") if q.strip()]
    if not queries:
        return "关键词为空。"
    out = []
    total = 0
    for q in queries:
        res = _search_one(q, engine, count)
        out.append(f"=== 关键词「{q}」 ===")
        if isinstance(res, dict) and res.get("error"):
            out.append(f"搜索失败：{res['error']}\n")
            continue
        if not res:
            out.append("（无结果，可能被反爬；换关键词或 engine=baidu 重试）\n")
            continue
        for i, r in enumerate(res, 1):
            out.append(f"{i}. {r['title']}")
            out.append(f"   链接: {r['url']}")
            if r.get("snippet"):
                out.append(f"   摘要: {r['snippet']}")
        out.append("")
        total += len(res)
    header = f"搜索 {len(queries)} 个关键词，参考 {total} 篇资料（引擎: {engine}）\n"
    tip = "\n提示：对重点结果用 fetch_web 抓取全文，再综合写文档。"
    return header + "\n".join(out) + tip

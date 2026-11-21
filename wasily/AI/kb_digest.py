# -*- coding: utf-8 -*-
"""知识库内容理解层：读取 → 结构化摘要 → 多文件聚合 → 报告。

复用原则：
- 已入库文件直接读 chunks_of_file，不重新解析、不触碰索引写路径；
- 未入库文件才回退 kb_parser.extract（只读抽取）；
- 摘要默认走确定性抽取式，保证离线可用；mode="llm" 时才调用本机模型。
"""
import json
import os
import re
import time
import urllib.request
from pathlib import Path

import kb_files
import kb_parser
import kb_search
import kb_store

BASE_DIR = Path(__file__).resolve().parent
REPORT_DIR = BASE_DIR / "outputs" / "kb_reports"
MAX_CONTENT_CHARS = 24000
API_BASE = os.environ.get("WASILY_API", "http://127.0.0.1:8000")


def _store():
    return kb_store.get_store()


# ---------------- 读取 ----------------
def _record(path=None, file_id=None):
    st = _store()
    if file_id not in (None, ""):
        return st.get_file(int(file_id))
    if path:
        return st.get_file_by_path(os.path.abspath(path))
    return None


def read_document(path=None, file_id=None, max_chars=MAX_CONTENT_CHARS,
                  allow_caption=False):
    """读取文件正文；优先复用索引分块，未入库再走解析器。"""
    rec = _record(path, file_id)
    if rec and rec.get("status") == "indexed":
        chunks = _store().chunks_of_file(rec["id"])
        text = "\n\n".join(c["text"] for c in chunks)
        source = "index"
        name, kind = rec["name"], rec["kind"]
        real_path = rec["path"]
        size, mtime = rec["size"], rec["mtime"]
        caption = rec.get("caption") or ""
    else:
        if not path:
            raise ValueError("文件不在索引中，且未提供路径")
        real_path = os.path.abspath(path)
        if not os.path.isfile(real_path):
            raise ValueError("文件不存在：%s" % real_path)
        kind = kb_parser.classify(real_path)
        if kind == "skip":
            raise ValueError("该格式不支持正文抽取（%s），可改用文件名检索"
                             % kb_files.classify_any(real_path))
        data = kb_parser.extract(real_path, kind, allow_caption=allow_caption)
        text = data.get("text") or ""
        caption = data.get("caption") or ""
        source = "parser"
        name = os.path.basename(real_path)
        size = os.path.getsize(real_path)
        mtime = os.path.getmtime(real_path)

    text = (text or "").strip()
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return {"ok": True, "path": real_path, "name": name, "kind": kind,
            "size": size, "mtime": mtime, "source": source,
            "text": text, "chars": len(text), "truncated": truncated,
            "caption": caption}


# ---------------- 抽取式摘要 ----------------
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|\n+")


def _terms(text):
    words = re.findall(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fa5]{2,4}", text or "")
    return [w.lower() for w in words]


def keywords(text, limit=8):
    stop = {"我们", "可以", "这个", "那个", "如果", "因为", "所以", "以及",
            "进行", "需要", "通过", "一个", "没有", "已经", "就是", "他们"}
    freq = {}
    for w in _terms(text):
        if w in stop or len(w) < 2:
            continue
        freq[w] = freq.get(w, 0) + 1
    return [w for w, _n in sorted(freq.items(), key=lambda x: -x[1])[:limit]]


def _sentences(text):
    out = []
    for part in _SENT_SPLIT.split(text or ""):
        s = re.sub(r"\s+", " ", part or "").strip()
        if 8 <= len(s) <= 400:
            out.append(s)
    return out


def extractive_summary(text, query="", max_points=6):
    """不依赖模型：按查询词覆盖度 + 位置加权挑句，保持原文顺序。"""
    sents = _sentences(text)
    if not sents:
        return []
    q_terms = set(_terms(query))
    scored = []
    for idx, s in enumerate(sents):
        low = s.lower()
        hit = sum(low.count(t) for t in q_terms) if q_terms else 0
        pos = 1.0 / (1 + idx * 0.12)
        length = min(len(s), 160) / 160.0
        scored.append((hit * 2.0 + pos + length * 0.4, idx, s))
    top = sorted(scored, key=lambda x: -x[0])[:max(1, int(max_points))]
    top.sort(key=lambda x: x[1])
    return [s for _score, _idx, s in top]


def _llm(prompt, max_tokens=700, timeout=180):
    body = json.dumps({
        "model": "local", "raw": True, "temperature": 0.2,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(API_BASE + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")


def _parse_json(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


def summarize(path=None, file_id=None, query="", mode="auto", max_points=6,
              allow_caption=False):
    """单文件结构化摘要。mode: auto=先抽取，llm 可用时再增强；extractive；llm。"""
    doc = read_document(path, file_id, allow_caption=allow_caption)
    text = doc["text"]
    result = {
        "ok": True, "path": doc["path"], "name": doc["name"], "kind": doc["kind"],
        "size": doc["size"], "mtime": doc["mtime"], "source": doc["source"],
        "chars": doc["chars"], "truncated": doc["truncated"], "query": query,
        "overview": "", "key_points": extractive_summary(text, query, max_points),
        "keywords": keywords(text), "mode": "extractive",
    }
    if not text:
        result["overview"] = "（无可抽取正文：该文件可能只有二进制内容或尚未完成索引）"
        result["key_points"] = []
        return result
    result["overview"] = "；".join(result["key_points"][:2])

    if mode in ("auto", "llm"):
        prompt = (
            "你是知识库摘要助手。请基于下面的文件内容生成结构化摘要，"
            "只输出 JSON，不要解释。格式："
            '{"overview":"两句话概述","key_points":["要点1","要点2"],'
            '"keywords":["关键词"]}\n'
            "要求：要点 3-6 条，贴合用户查询；不确定的内容不要编造。\n"
            "用户查询：%s\n文件名：%s\n内容：\n%s"
            % (query or "（无）", doc["name"], text[:12000])
        )
        try:
            parsed = _parse_json(_llm(prompt))
            points = [str(x).strip() for x in (parsed.get("key_points") or [])
                      if str(x).strip()]
            if points:
                result["overview"] = str(parsed.get("overview") or "").strip() \
                    or result["overview"]
                result["key_points"] = points[:max_points + 2]
                result["keywords"] = [str(x) for x in (parsed.get("keywords") or [])
                                      ][:10] or result["keywords"]
                result["mode"] = "llm"
        except Exception as exc:  # noqa: BLE001 —— 模型不可用时保留抽取结果
            result["llm_error"] = "%s: %s" % (type(exc).__name__, exc)
            if mode == "llm":
                result["mode"] = "extractive-fallback"
    return result


# ---------------- 多文件聚合 ----------------
def summarize_many(paths=None, query="", limit=5, mode="extractive",
                   max_points=4, allow_caption=False):
    """跨文件聚合总结：先单文件摘要，再合并主题与差异。"""
    items, errors = [], []
    for p in (paths or [])[:max(1, int(limit))]:
        try:
            items.append(summarize(path=p, query=query, mode=mode,
                                   max_points=max_points,
                                   allow_caption=allow_caption))
        except Exception as exc:  # noqa: BLE001
            errors.append({"path": p, "error": str(exc)})
    if not items and query:
        found = search_all(query, limit=limit)
        for r in found.get("results", []):
            try:
                items.append(summarize(path=r["path"], query=query, mode=mode,
                                       max_points=max_points,
                                       allow_caption=allow_caption))
            except Exception as exc:  # noqa: BLE001
                errors.append({"path": r["path"], "error": str(exc)})
    if not items:
        return {"ok": False, "error": "没有可总结的文件", "details": errors}

    kw = {}
    for it in items:
        for k in it.get("keywords", [])[:8]:
            kw[k] = kw.get(k, 0) + 1
    themes = [k for k, n in sorted(kw.items(), key=lambda x: -x[1])[:10] if n > 1]
    lines = []
    for it in items:
        point = (it.get("key_points") or [it.get("overview") or ""])[0]
        lines.append("%s：%s" % (it["name"], point[:160]))
    aggregate = {
        "ok": True, "query": query, "count": len(items),
        "files": [{"name": it["name"], "path": it["path"], "kind": it["kind"],
                   "overview": it["overview"], "key_points": it["key_points"]}
                  for it in items],
        "themes": themes,
        "summary": "\n".join(lines),
        "errors": errors,
    }
    if mode == "llm":
        prompt = (
            "你是资料分析助手。下面是多个文件的摘要，请输出 JSON："
            '{"summary":"跨文件总体结论","themes":["共同主题"],'
            '"differences":["差异或冲突"]}\n资料：\n%s'
            % json.dumps(aggregate["files"], ensure_ascii=False)[:12000]
        )
        try:
            parsed = _parse_json(_llm(prompt, max_tokens=900))
            aggregate["summary"] = str(parsed.get("summary") or aggregate["summary"])
            aggregate["themes"] = parsed.get("themes") or aggregate["themes"]
            aggregate["differences"] = parsed.get("differences") or []
            aggregate["mode"] = "llm"
        except Exception as exc:  # noqa: BLE001
            aggregate["llm_error"] = str(exc)
    return aggregate


def search_all(query, kind="", limit=8, include_names=True,
               kinds_extra=None):
    """全类型检索：存量索引混合检索 + 文件名补搜（音视频/压缩包等）。"""
    content = kb_search.search(query, kind=(kind or None), limit=limit)
    results = [dict(r, match="content") for r in (content.get("results") or [])]
    name_hits = []
    if include_names:
        wanted = []
        if kind and kind != "all":
            wanted = [kind]
        wanted += list(kinds_extra or [])
        if kind in ("audio", "video", "archive") or kinds_extra:
            name_hits = kb_files.search_names(query, kinds=wanted or None,
                                              limit=limit)["results"]
    seen = {r["path"] for r in results}
    for r in name_hits:
        if r["path"] not in seen:
            results.append(dict(r, match="name", snippets=[]))
    return {"ok": True, "query": query, "count": len(results),
            "results": results[:limit], "mode": content.get("mode"),
            "vector_error": content.get("vector_error")}


# ---------------- 报告 ----------------
def _slug(text):
    return re.sub(r"[^\w\u4e00-\u9fa5\-]+", "_", text or "report")[:60] or "report"


def build_report(topic, paths=None, query="", mode="extractive", fmt="markdown",
                 save=True):
    """把多文件总结落成 Markdown/HTML 报告。"""
    if paths:
        agg = summarize_many(paths=paths, query=query or topic, mode=mode)
    else:
        found = search_all(query or topic, limit=6)
        agg = summarize_many(paths=[r["path"] for r in found["results"]],
                             query=query or topic, mode=mode)
    if not agg.get("ok"):
        return agg
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    md = ["# %s" % topic, "", "> 生成时间：%s · 文件数：%d · 方式：%s"
          % (now, agg["count"], agg.get("mode", mode)), "", "## 总体结论", "",
          agg.get("summary") or "（无）", ""]
    if agg.get("themes"):
        md += ["**共同主题**：" + "、".join(agg["themes"]), ""]
    md += ["## 分文件摘要", ""]
    for it in agg["files"]:
        md += ["### %s" % it["name"], "",
               "- 类型：%s" % it["kind"], "- 路径：`%s`" % it["path"],
               "- 概述：%s" % (it.get("overview") or "（无）"), ""]
        for p in it.get("key_points") or []:
            md.append("- %s" % p)
        md.append("")
    md += ["---", "", "由 Wasily 知识库能力总线生成。"]
    text = "\n".join(md)
    if fmt == "html":
        html = ("<!DOCTYPE html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
                "<title>%s</title><style>body{max-width:860px;margin:40px auto;"
                "font:16px/1.8 system-ui,'Microsoft YaHei';padding:0 20px}"
                "code{background:#f2f4f8;padding:2px 6px;border-radius:4px}"
                "</style><body><pre style=\"white-space:pre-wrap;font:inherit\">"
                "%s</pre></body></html>" % (topic, _escape(text)))
        text = html
    out_path = ""
    if save:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        ext = "html" if fmt == "html" else "md"
        out_path = str(REPORT_DIR / ("%s-%s.%s"
                                     % (time.strftime("%Y%m%d-%H%M%S"),
                                        _slug(topic), ext)))
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
    return {"ok": True, "topic": topic, "format": fmt, "path": out_path,
            "content": text, "files": agg["count"]}


def _escape(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def stats():
    out = kb_search.stats()
    out["meta_kinds"] = [k["kind"] for k in kb_files.supported_kinds()]
    return {"ok": True, "stats": out}

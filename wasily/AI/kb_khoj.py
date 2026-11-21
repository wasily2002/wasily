# -*- coding: utf-8 -*-
"""可选 Khoj 后端适配层（默认不启用）。

Khoj 1.42 的服务端强依赖 PostgreSQL + pgvector（官方用 Docker 起库）；
本机没有 Docker/Postgres，所以默认走 kb_search 的本地实现。
用户自行装好 Khoj 后，在 config.json 里设置：
    "kb": {"backend": "khoj", "khoj": {"url": "http://127.0.0.1:42110"}}
即可让 Web/CLI/AI 工具复用 Khoj 的索引与检索结果；连不上时自动回退本地。
"""
import json
import urllib.request

import config


def enabled():
    try:
        return str(config.get("kb.backend", "local")).lower() == "khoj"
    except Exception:  # noqa: BLE001
        return False


def base_url():
    try:
        return str(config.get("kb.khoj.url", "http://127.0.0.1:42110")).rstrip("/")
    except Exception:  # noqa: BLE001
        return "http://127.0.0.1:42110"


def health(timeout=3):
    try:
        with urllib.request.urlopen(base_url() + "/api/health", timeout=timeout) as r:
            return {"ok": True, "status": r.status}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                "url": base_url()}


def search(query, limit=10, timeout=20):
    """调用 Khoj /api/search。返回与 kb_search.search 同构的结果，失败返回 None。"""
    if not enabled():
        return None
    body = json.dumps({"q": query, "n": int(limit), "t": "all"}).encode("utf-8")
    req = urllib.request.Request(base_url() + "/api/search", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 回退本地检索
        return None
    results = []
    for item in (data.get("data") or [])[:int(limit)]:
        additional = item.get("additional") or {}
        path = additional.get("file") or additional.get("path") or ""
        results.append({
            "file_id": None, "path": path,
            "name": path.replace("\\", "/").rsplit("/", 1)[-1],
            "kind": additional.get("file_type") or "khoj",
            "ext": "", "size": 0, "mtime": 0,
            "score": item.get("additional", {}).get("score") or 0,
            "matched_by": ["khoj"],
            "snippets": [{"ord": 0, "text": (item.get("entry") or "")[:300]}],
        })
    return {"ok": True, "query": query, "mode": "khoj", "count": len(results),
            "took_ms": 0, "results": results}

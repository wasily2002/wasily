# -*- coding: utf-8 -*-
"""知识库混合检索：BGE 向量近邻 + FTS5 全文 + RRF 融合。

跨类型统一：文本、PDF、Office 分块与图片（描述 + OCR 文字）都落在同一张
chunks 表里，检索时天然可以在一次查询中同时命中文件和图片。
"""
import re
import threading
import time
from urllib.parse import quote

import kb_store


def _encoder():
    # 复用索引器的编码器单例，避免重复拉起嵌入子进程
    import kb_index
    return kb_index._encoder()


def _rrf(rank_lists, k=60):
    """Reciprocal Rank Fusion：不依赖两路分数量纲，只吃名次，稳健。"""
    scores = {}
    src = {}
    for name, items in rank_lists:
        for rank, cid in enumerate(items):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            src.setdefault(cid, set()).add(name)
    return sorted(scores.items(), key=lambda x: -x[1]), src


def _snippet(text, query, width=240):
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    terms = [t for t in re.split(r"\s+", query or "") if t][:6]
    pos = -1
    for t in terms:
        p = text.lower().find(t.lower())
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
    if pos < 0:
        return text[:width]
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


_WARM_THREAD = None
_WARM_LOCK = threading.Lock()


def _warm_run():
    """预热两件事：向量矩阵（整库一次加载）+ 嵌入子进程（模型加载约 30s）。"""
    store = kb_store.get_store()
    try:
        store.vector_cache()
    except Exception:  # noqa: BLE001
        pass
    try:
        _encoder()
    except Exception:  # noqa: BLE001
        pass


def warm_vector_cache():
    """后台预加载：整库向量 + BGE 嵌入进程，避免用户第一次检索等 40 秒。
    由 /v1/kb/status 轮询触发（页面打开时正好预热）。"""
    global _WARM_THREAD
    store = kb_store.get_store()
    if store.vector_cache_ready():
        return False
    with _WARM_LOCK:
        if _WARM_THREAD is not None and _WARM_THREAD.is_alive():
            return False
        _WARM_THREAD = threading.Thread(target=_warm_run, name="kb-vec-warm",
                                        daemon=True)
        _WARM_THREAD.start()
        return True


def search(query, kind=None, limit=10, per_file=2):
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "查询不能为空"}
    # 配置了 Khoj 后端且它可用时优先走 Khoj；失败自动回退本地混合检索
    try:
        import kb_khoj
        remote = kb_khoj.search(q, limit=limit)
        if remote:
            return remote
    except Exception:  # noqa: BLE001
        pass
    t0 = time.time()
    store = kb_store.get_store()
    cand = max(60, int(limit) * 8)
    vlist, flist, mode = [], [], "hybrid"
    vec_err = ""
    try:
        qvec = _encoder().query(q)
        vlist = [cid for _s, cid in store.knn(qvec, cand)]
    except Exception as exc:  # noqa: BLE001 嵌入不可用时降级为纯全文
        vec_err = "%s: %s" % (type(exc).__name__, exc)
        mode = "fts"
    flist = [cid for cid, _s in store.fts(q, cand)]
    fused, src = _rrf([("vec", vlist), ("fts", flist)])

    # 取候选块与文件信息，按文件聚合
    picked = fused[:cand]
    chunks = store.chunks_by_ids([cid for cid, _ in picked])
    files = store.files_by_ids([c["file_id"] for c in chunks.values()])
    groups = {}
    for cid, score in picked:
        ch = chunks.get(cid)
        if not ch:
            continue
        f = files.get(ch["file_id"])
        if not f or f["status"] != "indexed":
            continue
        if kind and kind != "all" and f["kind"] != kind:
            continue
        g = groups.setdefault(f["id"], {"file": f, "score": score, "chunks": []})
        if len(g["chunks"]) < per_file:
            g["chunks"].append(ch)

    results = []
    for g in groups.values():
        f = g["file"]
        snippets = [{"ord": c["ord"], "text": _snippet(c["text"], q)}
                    for c in g["chunks"]]
        item = {
            "file_id": f["id"], "path": f["path"], "name": f["name"],
            "kind": f["kind"], "ext": f["ext"], "size": f["size"],
            "mtime": f["mtime"], "score": round(g["score"], 5),
            "matched_by": sorted(src.get(g["chunks"][0]["id"], [])),
            "snippets": snippets,
        }
        if f["kind"] == "image":
            item["caption"] = f.get("caption") or ""
            item["thumb"] = "/v1/kb/thumb?path=" + quote(f["path"])
            item["raw"] = "/v1/kb/file?path=" + quote(f["path"])
        else:
            item["open"] = "/v1/kb/file?path=" + quote(f["path"])
        results.append(item)
    results.sort(key=lambda x: -x["score"])
    return {"ok": True, "query": q, "mode": mode, "vector_error": vec_err or None,
            "count": len(results), "took_ms": int((time.time() - t0) * 1000),
            "results": results[:int(limit)]}


def stats():
    store = kb_store.get_store()
    out = store.status_counts()
    out["db_mb"] = round(store.db_size() / 1048576, 1)
    out["last_run"] = store.meta_get("kb.last_run")
    return out

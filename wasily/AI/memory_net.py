# -*- coding: utf-8 -*-
"""
多层记忆网络（L0-L3 卷积式记忆金字塔）
  L0 原始层：history.enc 全量对话（现有，永久保留）
  L1 摘要层：会话被截断丢弃的消息 → 模型压缩为会话摘要
  L2 主题层：多条 L1 摘要 → 归并为长期事实
  L3 自我层：多条 L2 事实 → 提炼核心认知（关于用户与自我）
读取：recall(query) 以本地向量语义召回为主、关键词为辅，注入系统提示词。
"""
import json
import re
import threading
import time
from pathlib import Path

import config
from memory_vector import MemoryVectorStore, VectorEncoder
from secure_store import EncLines

_DIR = Path(__file__).resolve().parent
_LAYERS = EncLines(str(_DIR / "mem_layers.enc"))    # {id,layer,topic,text,ts,hits}
_PENDING_STORE = EncLines(str(_DIR / "mem_pending.enc"))  # {id,sid,role,content,ts}
_LOCK = threading.Lock()
_COMPRESSING = {"running": False}
_VECTOR_ENCODER = VectorEncoder()
_VECTOR_STORE = MemoryVectorStore(encoder=_VECTOR_ENCODER)
_VECTOR_INDEXING = {"running": False}
# 召回降级原因：向量分支静默回退关键词时，至少要能从 /memory/layers 看到原因
_VECTOR_RECALL = {"error": ""}

# 压缩阈值
PENDING_TRIGGER = 12    # 待压缩消息攒够 12 条触发 L1 压缩
L1_TRIGGER = 6          # L1 超过 6 条 → 最旧 4 条归并为 L2
L2_TRIGGER = 8          # L2 超过 8 条 → 最旧 4 条提炼为 L3
CONSOLIDATE_N = 4


# ---------- LLM 访问（延迟导入避免循环依赖） ----------
def _llm(prompt: str, max_tokens=400) -> str:
    from api_server import generate_text
    return generate_text(
        [{"role": "user", "content": prompt}],
        max_tokens=max_tokens, temperature=0.3).strip()


# ---------- 写入路径：截断消息 → L1 ----------
def ingest(session_id: str, messages: list):
    """接收会话中被硬截断丢弃的消息，攒够阈值后后台压缩为 L1 摘要"""
    if not messages:
        return
    for m in messages:
        if m.get("role") == "system":
            continue
        _PENDING_STORE.append({
            "id": _PENDING_STORE.next_id(), "sid": session_id,
            "role": m.get("role", ""), "content": str(m.get("content", ""))[:2000],
            "ts": time.time()})
    n = sum(1 for _ in _iter_pending())
    if n >= PENDING_TRIGGER:
        start_compress()


def _iter_pending():
    return _PENDING_STORE.load_all()


def start_compress():
    """后台启动一次压缩（若未在运行）"""
    if _COMPRESSING["running"]:
        return False
    _COMPRESSING["running"] = True
    threading.Thread(target=_compress_worker, daemon=True).start()
    return True


def _compress_worker():
    try:
        # 压缩是重 LLM 任务：等对话空闲 45s 以上再跑，避免抢占推理 CPU 拖慢回复
        waited = 0
        while waited < 600:
            try:
                from api_server import AGENT_STATE
                if (AGENT_STATE.get("phase") == "idle"
                        and time.time() - float(AGENT_STATE.get("since", 0) or 0) > 45):
                    break
            except Exception:
                break
            time.sleep(10)
            waited += 10
        pend = _iter_pending()
        if not pend:
            return
        ids = [p["id"] for p in pend]
        text = "\n".join(
            f"{'用户' if p['role'] == 'user' else '助手'}: {p['content'][:400]}"
            for p in pend[-PENDING_TRIGGER * 2:])  # 最多取 24 条防爆
        out = _llm(
            "你是记忆压缩器。把下面的多轮对话压缩为一段不超过150字的会话摘要，"
            "保留关键事实、决定、偏好与结论，忽略寒暄。最后一行以【主题】开头给出2-6字主题标签。\n\n"
            + text, max_tokens=300)
        topic = "未分类"
        mt = re.search(r"【主题】[:：]?\s*(\S{1,12})", out)
        if mt:
            topic = mt.group(1).strip("。，,.")
        summary = re.sub(r"【主题】.*", "", out).strip()[:600] or out[:600]
        _add_entry(1, topic, summary, src=f"{len(ids)}条消息")
        _PENDING_STORE.delete_ids(ids)
        _consolidate()
    except Exception as e:
        print(f"[memory_net] 压缩失败: {e}", flush=True)
    finally:
        _COMPRESSING["running"] = False


def _vector_enabled():
    return bool(config.get("memory.vector.enabled", True))


def start_reconcile():
    """空闲后台补建旧记忆索引，模型未就绪时不阻塞对话。"""
    if not _vector_enabled() or _VECTOR_INDEXING["running"]:
        return False
    _VECTOR_INDEXING["running"] = True
    threading.Thread(target=_reconcile_worker, daemon=True).start()
    return True


def _reconcile_worker():
    try:
        batch = max(1, int(config.get("memory.vector.batch_size", 16)))
        result = _VECTOR_STORE.reconcile(_LAYERS.load_all(), batch_size=batch)
        print(f"[memory_net] 向量索引对账: {result}", flush=True)
    except Exception as e:
        print(f"[memory_net] 向量索引降级为关键词召回: {e}", flush=True)
    finally:
        _VECTOR_INDEXING["running"] = False


def _delete_vectors(entry_ids):
    try:
        _VECTOR_STORE.delete(entry_ids)
    except Exception as e:
        # 下次对账会清理孤儿索引，不能让索引故障阻断遗忘正文
        print(f"[memory_net] 向量删除待对账: {e}", flush=True)


def _norm_text(text):
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().casefold()
    return normalized.strip("。！？!?，,；;：:、")


def _add_entry(layer: int, topic: str, text: str, src=""):
    normalized = _norm_text(text)
    recs = _LAYERS.load_all()
    for r in recs:
        if (r.get("layer") == layer and r.get("topic", "") == topic
                and _norm_text(r.get("text")) == normalized):
            return r.get("id")

    vec = None
    if _vector_enabled() and normalized:
        try:
            body = (topic + " " + text).strip()
            vec = _VECTOR_ENCODER.documents([body], batch_size=1)[0]
            candidates = [r for r in recs if r.get("layer") == layer
                          and r.get("topic", "") == topic]
            if candidates:
                near = _VECTOR_STORE.search(
                    vec, top=1, allowed_ids={r["id"] for r in candidates})
                threshold = float(config.get("memory.vector.dedupe", 0.96))
                if near and near[0][1] >= threshold:
                    return near[0][0]
        except Exception as e:
            print(f"[memory_net] 向量写入降级，稍后对账: {e}", flush=True)

    with _LOCK:
        # 编码可能耗时，落盘前再查一次，避免并发写入重复正文
        recs = _LAYERS.load_all()
        for r in recs:
            if (r.get("layer") == layer and r.get("topic", "") == topic
                    and _norm_text(r.get("text")) == normalized):
                return r.get("id")
        entry_id = _LAYERS.next_id(recs)
        _LAYERS.append({"id": entry_id, "layer": layer, "topic": topic,
                        "text": text, "ts": time.time(), "hits": 0, "src": src})
    if vec is not None:
        try:
            _VECTOR_STORE.upsert(entry_id, vec)
        except Exception as e:
            print(f"[memory_net] 向量暂存失败，等待对账: {e}", flush=True)
    return entry_id


def _consolidate():
    """卷积归并：L1→L2→L3 逐级抽象"""
    recs = _LAYERS.load_all()
    for low, high, trigger, hint in (
            (1, 2, L1_TRIGGER, "把以下会话摘要归并提炼为一条不超过200字的长期事实，保留稳定结论与偏好："),
            (2, 3, L2_TRIGGER, "把以下长期事实提炼为一条不超过150字的核心认知（关于用户的偏好/习惯/身份或系统自身能力）：")):
        group = sorted([r for r in recs if r.get("layer") == low], key=lambda r: r.get("ts", 0))
        if len(group) <= trigger:
            continue
        olds = group[:CONSOLIDATE_N]
        try:
            merged = _llm(hint + "\n\n" + "\n".join(
                f"- [{o.get('topic','')}] {o['text']}" for o in olds), max_tokens=300)
            _add_entry(high, olds[0].get("topic", "未分类"), merged[:600],
                       src=f"L{low}×{len(olds)}")
            old_ids = [o["id"] for o in olds]
            _LAYERS.delete_ids(old_ids)
            _delete_vectors(old_ids)
            recs = _LAYERS.load_all()
        except Exception as e:
            print(f"[memory_net] L{low}→L{high} 归并失败: {e}", flush=True)
            break


# ---------- 读取路径：联想召回 ----------
def _terms(text: str):
    """提取查询关键词：连续中文/英文词 + 二元字组"""
    words = re.findall(r"[A-Za-z0-9_]{2,}|[一-鿿]+", text.lower())
    terms = set()
    for w in words:
        if re.fullmatch(r"[一-鿿]+", w):
            terms.update(w[i:i + 2] for i in range(len(w) - 1))
            if len(w) >= 4:
                terms.add(w)
        else:
            terms.add(w)
    return terms


def _keyword_scores(query, recs):
    qs = _terms(query)
    if not qs:
        return {}
    weight = {3: 3.0, 2: 2.0, 1: 1.0}
    scores = {}
    for r in recs:
        body = (r.get("topic", "") + " " + r.get("text", "")).lower()
        hits = sum(1 for t in qs if t in body)
        if hits:
            scores[r["id"]] = (hits * weight.get(r.get("layer", 1), 1.0)
                               + min(r.get("hits", 0), 10) * 0.1)
    return scores


def _strengthen(out):
    if not out:
        return
    ids = {r["id"] for r in out}
    recs = _LAYERS.load_all()
    changed = False
    for r in recs:
        if r.get("id") in ids:
            r["hits"] = r.get("hits", 0) + 1
            changed = True
    if changed:
        _LAYERS.rewrite(recs)


def recall(query: str, top=None) -> list:
    """向量语义为主、关键词为辅；索引未就绪时保持旧召回行为。"""
    if not str(query or "").strip():
        return []
    recs = _LAYERS.load_all()
    if not recs:
        return []
    top = max(1, int(top if top is not None else config.get("memory.vector.top_k", 5)))
    keyword = _keyword_scores(query, recs)
    by_id = {r["id"]: r for r in recs}
    ranked = []
    try:
        if not _vector_enabled():
            raise RuntimeError("向量记忆已禁用")
        indexed = _VECTOR_STORE.indexed_ids()
        if not all(r["id"] in indexed for r in recs):
            raise RuntimeError("向量索引尚未完成")
        query_vec = _VECTOR_ENCODER.query(query)
        min_score = float(config.get("memory.vector.min_score", 0.45))
        for entry_id, cosine in _VECTOR_STORE.search(query_vec, top=len(recs)):
            if cosine < min_score:
                continue
            # 关键词只作小幅加权，不能把无关记忆顶过语义阈值
            score = cosine + min(keyword.get(entry_id, 0), 12.0) * 0.025
            ranked.append((score, entry_id))
        _VECTOR_RECALL["error"] = ""
    except Exception as e:
        _VECTOR_RECALL["error"] = f"{type(e).__name__}: {e}"
        ranked = [(score, entry_id) for entry_id, score in keyword.items()]
    ranked.sort(key=lambda item: item[0], reverse=True)
    out = [by_id[entry_id] for _, entry_id in ranked[:top] if entry_id in by_id]
    _strengthen(out)
    return out


_LNAME = {1: "会话摘要", 2: "长期事实", 3: "核心认知"}


def recall_section(query: str, top=None) -> str:
    """生成注入系统提示词的联想记忆段"""
    inject_top = top if top is not None else config.get("memory.vector.inject_top", 3)
    rs = recall(query, inject_top)
    if not rs:
        return ""
    lines = ["", "【联想记忆】（与当前话题相关的历史记忆，供参考）"]
    for r in rs:
        lines.append(f"- [{_LNAME.get(r.get('layer', 1))}/{r.get('topic', '')}] "
                     f"{r.get('text', '')[:220]}")
    return "\n".join(lines)


def stats() -> dict:
    recs = _LAYERS.load_all()
    by = {1: 0, 2: 0, 3: 0}
    for r in recs:
        by[r.get("layer", 1)] = by.get(r.get("layer", 1), 0) + 1
    result = {"L1_会话摘要": by[1], "L2_长期事实": by[2], "L3_核心认知": by[3],
              "待压缩消息": len(_iter_pending()), "压缩中": _COMPRESSING["running"],
              "向量启用": _vector_enabled(), "向量索引中": _VECTOR_INDEXING["running"]}
    try:
        vs = _VECTOR_STORE.stats({r["id"] for r in recs})
        result.update({"向量模型": vs["model"], "向量已索引": vs["indexed"],
                       "向量待索引": vs.get("pending", len(recs)),
                       "向量就绪": vs["ready"], "向量错误": vs["error"],
                       "向量召回降级": _VECTOR_RECALL["error"]})
    except Exception as e:
        result.update({"向量模型": _VECTOR_ENCODER.model_name, "向量已索引": 0,
                       "向量待索引": len(recs), "向量就绪": False,
                       "向量错误": str(e), "向量召回降级": _VECTOR_RECALL["error"]})
    return result


def layers_view(limit=50) -> list:
    recs = sorted(_LAYERS.load_all(), key=lambda r: -r.get("ts", 0))[:limit]
    return [{"id": r.get("id"), "layer": r.get("layer"), "topic": r.get("topic"),
             "text": r.get("text", "")[:300], "hits": r.get("hits", 0),
             "src": r.get("src", ""),
             "time": time.strftime("%m-%d %H:%M", time.localtime(r.get("ts", 0)))}
            for r in recs]


def forget(entry_id: int) -> int:
    removed = _LAYERS.delete_ids([entry_id])
    if removed:
        _delete_vectors([entry_id])
    return removed

# -*- coding: utf-8 -*-
"""训练数据闭环：采集 → 清洗 → 标注 → 导出回流。

数据全部落在 outputs/training，采用 JSONL，便于增量与外部训练框架直接读取。
采集默认读取 sessions_store（不复制原始会话，只抽取问答对），
也支持 train_append 实时写入。
"""
import hashlib
import json
import os
import re
import threading
import time

import config as cfg

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRAIN_DIR = os.path.join(BASE_DIR, "outputs", "training")
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions_store")

EVENTS = os.path.join(TRAIN_DIR, "events.jsonl")
DATASET = os.path.join(TRAIN_DIR, "dataset.jsonl")
LABELED = os.path.join(TRAIN_DIR, "labeled.jsonl")
TRAIN = os.path.join(TRAIN_DIR, "train.jsonl")
VAL = os.path.join(TRAIN_DIR, "val.jsonl")
FEEDBACK = os.path.join(TRAIN_DIR, "feedback.jsonl")
MANIFEST = os.path.join(TRAIN_DIR, "manifest.json")

TOOL_RE = re.compile(r'"name"\s*:\s*"([a-zA-Z0-9_]+)"|\[工具调用[^\]]*\]\s*'
                     r'([a-zA-Z0-9_]+)\(|([a-zA-Z0-9_]+)\(\{[^}]*\}\)')
SECRET_RE = re.compile(
    r"(?i)(sk-[a-z0-9]{10,}|bearer\s+[a-z0-9._\-]+|"
    r"(?:password|passwd|api[_-]?key|secret|token)\s*[=:]\s*\S+)")
_STARTED = {"on": False}


def _ensure():
    os.makedirs(TRAIN_DIR, exist_ok=True)


def _hash(*parts):
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p or "").encode("utf-8", "ignore"))
    return h.hexdigest()


def _read_jsonl(path):
    if not os.path.isfile(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _write_jsonl(path, rows):
    _ensure()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _append_jsonl(path, rows):
    _ensure()
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def extract_tools(text):
    names = set()
    for m in TOOL_RE.finditer(text or ""):
        for g in m.groups():
            if g and not g.startswith(("http", "www")):
                names.add(g)
    return sorted(names)


# ---------------- 采集 ----------------
def _pairs_from_session(data):
    msgs = data.get("messages") or []
    out, pending = [], ""
    for i, m in enumerate(msgs):
        role = m.get("role")
        content = str(m.get("content") or "").strip()
        if role == "user":
            pending = content
        elif role == "assistant" and pending and content:
            out.append((pending, content, i))
            pending = ""
    return out


def collect(limit=2000):
    """从历史会话增量采集问答对；重复执行不会产生重复事件。"""
    _ensure()
    seen = {r.get("id") for r in _read_jsonl(EVENTS)}
    files = []
    if os.path.isdir(SESSIONS_DIR):
        for name in os.listdir(SESSIONS_DIR):
            if name.endswith(".json"):
                fp = os.path.join(SESSIONS_DIR, name)
                files.append((os.path.getmtime(fp), fp))
    files.sort(reverse=True)
    added, scanned = 0, 0
    rows = []
    for _mtime, fp in files:
        if added >= limit:
            break
        scanned += 1
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        sid = data.get("sid") or os.path.splitext(os.path.basename(fp))[0]
        updated = data.get("updated") or os.path.getmtime(fp)
        for input_text, output_text, idx in _pairs_from_session(data):
            eid = _hash(sid, idx, input_text[:120], output_text[:120])
            if eid in seen:
                continue
            seen.add(eid)
            rows.append({
                "id": eid, "ts": updated, "session_id": sid, "source": "session",
                "input": input_text, "output": output_text,
                "tools": extract_tools(output_text),
            })
            added += 1
    if rows:
        _append_jsonl(EVENTS, rows)
    return {"ok": True, "added": added, "scanned": scanned,
            "total_events": len(seen)}


def append_event(input_text, output_text, tools=None, session_id="", source="runtime"):
    """实时采集入口：接入方在完成一次交互后调用。"""
    if not str(input_text or "").strip() or not str(output_text or "").strip():
        raise ValueError("input/output 不能为空")
    eid = _hash(session_id, time.time(), input_text[:120], output_text[:120])
    row = {"id": eid, "ts": time.time(), "session_id": session_id or "runtime",
           "source": source, "input": str(input_text), "output": str(output_text),
           "tools": list(tools or extract_tools(output_text))}
    _append_jsonl(EVENTS, [row])
    return {"ok": True, "event": row}


# ---------------- 清洗 ----------------
def _redact(text):
    return SECRET_RE.sub("[REDACTED]", text or "")


def clean(min_chars=None, max_chars=None):
    """去空、去过短、去重、脱敏，生成 dataset.jsonl。"""
    min_chars = int(min_chars or cfg.get("training.min_chars", 8) or 8)
    max_chars = int(max_chars or cfg.get("training.max_chars", 20000) or 20000)
    rows, seen, dropped = [], set(), {"empty": 0, "short": 0, "dup": 0, "long": 0}
    for e in _read_jsonl(EVENTS):
        inp = _redact(str(e.get("input") or "")).strip()
        out = _redact(str(e.get("output") or "")).strip()
        if not inp or not out:
            dropped["empty"] += 1
            continue
        if len(out) < min_chars:
            dropped["short"] += 1
            continue
        if len(inp) + len(out) > max_chars:
            dropped["long"] += 1
            continue
        key = _hash(re.sub(r"\s+", " ", inp.lower()),
                    re.sub(r"\s+", " ", out.lower()))
        if key in seen:
            dropped["dup"] += 1
            continue
        seen.add(key)
        rows.append({"id": e.get("id") or key, "ts": e.get("ts"),
                     "session_id": e.get("session_id"), "source": e.get("source"),
                     "input": inp, "output": out,
                     "tools": e.get("tools") or extract_tools(out)})
    _write_jsonl(DATASET, rows)
    return {"ok": True, "kept": len(rows), "dropped": dropped,
            "path": DATASET}


# ---------------- 标注 ----------------
_LABEL_RULES = [
    ("code", ("app_code_", "ide_edit", "shell_exec", "run_python", "apply_patch",
              "file_write", "code_search")),
    ("office", ("app_office_",)),
    ("browser", ("browser_", "playwright")),
    ("knowledge", ("kb_", "train_", "memory_")),
]


def _heuristic_label(row):
    tools = row.get("tools") or []
    task = "other"
    for name, prefixes in _LABEL_RULES:
        if any(t.startswith(p) or p in t for t in tools for p in prefixes):
            task = name
            break
    out = row.get("output") or ""
    failed = any(k in out for k in ("调用失败", "工具错误", "Traceback", "执行失败"))
    success = not failed
    score = 0.4
    if success:
        score += 0.3
    if 50 <= len(out) <= 4000:
        score += 0.2
    if tools:
        score += 0.1
    return {"task_type": task, "success": success,
            "quality": round(min(score, 1.0), 2), "tools": tools}


def label(mode="heuristic"):
    """给 dataset 打标签：任务类型、成功与否、质量分。"""
    rows = _read_jsonl(DATASET)
    if not rows:
        return {"ok": False, "error": "dataset 为空，请先执行 train_clean"}
    out = []
    for r in rows:
        meta = _heuristic_label(r)
        out.append(dict(r, label=meta))
    _write_jsonl(LABELED, out)
    dist = {}
    for r in out:
        key = r["label"]["task_type"]
        dist[key] = dist.get(key, 0) + 1
    return {"ok": True, "labeled": len(out), "mode": "heuristic",
            "distribution": dist, "path": LABELED}


# ---------------- 导出 ----------------
def _to_sft(row):
    return {"messages": [
        {"role": "user", "content": row.get("input") or ""},
        {"role": "assistant", "content": row.get("output") or ""}],
        "meta": {"id": row.get("id"), "session_id": row.get("session_id"),
                 "source": row.get("source"), "tools": row.get("tools") or [],
                 "task_type": (row.get("label") or {}).get("task_type"),
                 "quality": (row.get("label") or {}).get("quality")}}


def export(val_ratio=None):
    """划分训练/验证集，输出可直接用于 SFT 的 JSONL。"""
    rows = _read_jsonl(LABELED) or _read_jsonl(DATASET)
    if not rows:
        return {"ok": False, "error": "没有可导出的数据，请先执行 clean/label"}
    ratio = float(val_ratio if val_ratio is not None
                  else cfg.get("training.val_ratio", 0.1) or 0.1)
    ratio = max(0.0, min(ratio, 0.5))
    train_rows, val_rows = [], []
    for r in rows:
        bucket = int(_hash(r.get("id") or "")[:8], 16) % 100
        (val_rows if bucket < ratio * 100 else train_rows).append(_to_sft(r))
    _write_jsonl(TRAIN, train_rows)
    _write_jsonl(VAL, val_rows)
    manifest = {"generated_at": time.time(), "total": len(rows),
                "train": len(train_rows), "val": len(val_rows),
                "val_ratio": ratio,
                "files": {"train": TRAIN, "val": VAL, "dataset": DATASET,
                          "events": EVENTS, "feedback": FEEDBACK}}
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return {"ok": True, "manifest": manifest}


# ---------------- 反馈回流 ----------------
def feedback(event_id, rating, comment=""):
    try:
        rating = max(0, min(int(rating), 5))
    except (TypeError, ValueError):
        raise ValueError("rating 需为 0-5 的整数")
    if not event_id:
        raise ValueError("缺少 event_id")
    row = {"event_id": str(event_id), "rating": rating,
           "comment": str(comment or ""), "ts": time.time()}
    _append_jsonl(FEEDBACK, [row])
    return {"ok": True, "feedback": row}


def stats():
    def count(path):
        return len(_read_jsonl(path)) if os.path.isfile(path) else 0

    manifest = {}
    if os.path.isfile(MANIFEST):
        try:
            with open(MANIFEST, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            manifest = {}
    dist = {}
    for r in _read_jsonl(LABELED):
        key = (r.get("label") or {}).get("task_type") or "unknown"
        dist[key] = dist.get(key, 0) + 1
    return {"ok": True, "dir": TRAIN_DIR, "events": count(EVENTS),
            "dataset": count(DATASET), "labeled": count(LABELED),
            "train": count(TRAIN), "val": count(VAL),
            "feedback": count(FEEDBACK), "distribution": dist,
            "manifest": manifest,
            "auto_collect": bool(cfg.get("training.auto_collect", True))}


def run_pipeline():
    c = collect()
    cl = clean()
    lb = label()
    ex = export()
    return {"ok": True, "collect": c, "clean": cl, "label": lb, "export": ex}


# ---------------- 自动采集 ----------------
def start_auto_collect():
    """kb 服务启动后按间隔增量采集；重复调用只启动一次。"""
    if _STARTED["on"] or not bool(cfg.get("training.auto_collect", True)):
        return False
    _STARTED["on"] = True
    interval = max(60, int(cfg.get("training.interval_sec", 600) or 600))

    def _loop():
        while True:
            try:
                collect()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(interval)

    threading.Thread(target=_loop, name="train-auto-collect", daemon=True).start()
    return True

# -*- coding: utf-8 -*-
"""会话内多层上下文压缩（卷积式池化）
保证单次任务无论跑多少轮工具、读多大的文件，注入模型的输入都落在上下文窗口内。

层级对应感受野/池化：
  L0 原文层   最近 keep_recent 条消息，全分辨率保留（浅层特征）
  L1 局部池化  较早的工具结果 → 首尾片段 + 关键文件/报错抽取（降采样）
  L2 区域池化  多条较早消息 → 每条一行摘要移出消息队列，写入会话历史块
  L3 全局池化  历史块超限 → 最旧若干条再合并（感受野逐层扩大）
  L4 硬裁剪   仍超预算 → 从最旧开始丢弃；末条超长则首尾截断
  L5 外部记忆 被挤出的原文落盘成流水账（ctx_journal），按当前话题召回注入 → 上下文可无限长

对外接口：
  set_system(sid, text)                     记录系统提示词基座（不含压缩历史）
  fit(sid, messages, budget, keep_recent)   原地压缩，返回被移出的原始消息
  shrink_tool_payload(text, limit)          单条工具结果注入前的首尾截断
  history_block(sid)                        渲染历史块（含在系统提示词里）
  stats(sid) / reset(sid)
"""
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uff00-\uffef\u3000-\u303f]")
_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|\.{1,2}/)[^\s\"'<>|\n\r]{2,160}")
_ERR_RE = re.compile(r"[^\n]*(?:错误|失败|异常|报错|Error|error|Traceback|Exception)[^\n]*")
_TERM_RE = re.compile(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]+")
_TOOL_TAG = "工具执行结果："
HISTORY_MARK = "\n\n## 本任务已压缩的历史（较早过程，仅保留要点）"
RECALL_MARK = "\n\n## 长期记忆召回（较早被压缩掉的过程，命中当前话题）"

HISTORY_MAX_TOKENS = 900      # 历史块自身上限（L3 池化阈值）
HISTORY_MAX_ITEMS = 10        # 历史块最多条目数
TOOL_KEEP_CHARS = 1500        # 单条工具结果注入上限（字符）
GOAL_MAX_CHARS = 400          # 任务目标（第一条用户诉求）保留原文上限

# ---- 无限长度思考：把池化挤出去的原文落盘成流水账，按话题按需召回 ----
JOURNAL_DIR = Path(__file__).resolve().parent / "ctx_journal"
JOURNAL_ENTRY_CHARS = 1600    # 单条流水账保留的原文长度
JOURNAL_MAX_BYTES = 8 * 1024 * 1024   # 单会话流水账上限，超了折半轮转
JOURNAL_TAIL_LINES = 3000     # 召回时只扫最近 N 条，控制检索耗时
RECALL_MAX_TOKENS = 420       # 召回块注入上限（token）
RECALL_TOP = 6                # 召回条数上限

# ---- 折叠对话：按窗口占用分级触发，把最早若干轮折叠成结构化摘要（与 L1-L5 同一套流水线）----
# 阈值沿用「用到窗口多少」的口径：60% 软折叠、80% 硬折叠；目标压回 55% / 50%。
FOLD_DIR = JOURNAL_DIR / "folds"


def _env_ratio(name, default):
    try:
        v = float(os.environ.get(name, "") or default)
        return v if 0 < v < 1 else default
    except Exception:
        return default


FOLD_WARN_RATIO = _env_ratio("WASILY_FOLD_WARN_RATIO", 0.60)   # 软阈值（窗口占用）
FOLD_HARD_RATIO = _env_ratio("WASILY_FOLD_HARD_RATIO", 0.80)   # 硬阈值（窗口占用）
FOLD_SOFT_TARGET = _env_ratio("WASILY_FOLD_SOFT_TARGET", 0.55)
FOLD_HARD_TARGET = _env_ratio("WASILY_FOLD_HARD_TARGET", 0.50)
FOLD_MIN_ROUNDS = 2           # 软折叠至少折这么多轮
FOLD_KEEP_ROUNDS = 2          # 始终保留最近这么多轮原文
FOLD_ITEM_CHARS = 900         # 折叠记录里单条原文保留上限（供界面展开，不再注入模型）
FOLD_MAX_ITEMS = 40           # 单次折叠最多保留多少条原文
FOLD_HIST_PREFIX = "[折叠摘要"   # 历史块里的折叠摘要条目标记（fit/池化时受保护）
# 折叠摘要渲染在系统提示词【头部】：下游 _shrink_messages 超窗时只保留头部，
# 放尾部会被整段裁掉，导致「原文已移除、摘要也没进模型」。
FOLD_HEAD_BEGIN = "[[WASILY_FOLD_SUMMARY_BEGIN]]"
FOLD_HEAD_END = "[[WASILY_FOLD_SUMMARY_END]]"
FOLD_HEAD_MARK = ("\n\n## 已折叠的历史对话摘要（较早过程仍须遵守其中的决定与未完成事项）\n")

_FILE_EXT_RE = re.compile(
    r"[A-Za-z0-9_\-./\\]{2,180}\.(?:py|js|mjs|cjs|ts|tsx|jsx|html?|css|scss|less|"
    r"json|jsonl|md|ya?ml|toml|ini|cfg|conf|java|go|rs|c|cc|cpp|h|hpp|cs|php|rb|"
    r"swift|kt|sh|ps1|bat|cmd|sql|txt|xml|vue|svelte|astro|gradle|lua|dart)\b")
_DECISION_RE = re.compile(
    r"[^\n。；;]{0,50}(?:决定|已确认|确定|采用|选择|敲定|最终方案|结论是|结论：|方案是|"
    r"就这么|按这个)[^\n。；;]{0,90}")
_TODO_RE = re.compile(
    r"[^\n。；;]{0,50}(?:待办|TODO|待确认|待处理|未完成|尚未|还没有|下一步|后续|还需要|"
    r"未解决|剩下|等待)[^\n。；;]{0,90}")
_ACT_NEW_RE = re.compile(r"(?:创建|新建|新增|写入|生成|添加|落盘)[^\n]{0,18}$")
_ACT_MOD_RE = re.compile(r"(?:修改|更改|更新|重构|修复|调整|编辑|改写|删除|重命名|迁移)[^\n]{0,18}$")

_LOCK = threading.RLock()
_SESS = {}                    # sid -> {"base": str, "goal": str, "hist": [{"text","tokens"}], "stats": {...}}


# ---------------- token 估算 ----------------
def estimate_tokens(text=""):
    s = "" if text is None else str(text)
    if not s:
        return 0
    cjk = len(_CJK_RE.findall(s))
    return int(cjk * 1.05 + (len(s) - cjk) / 3.0) + 1


def estimate_messages(messages):
    if not messages:
        return 0
    return sum(estimate_tokens(m.get("content", "")) for m in messages) + 4 * len(messages)


# ---------------- 抽取式摘要 ----------------
def _one_line(text, limit):
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _uniq(items):
    out = []
    for x in items:
        if x not in out:
            out.append(x)
    return out


def _strip_toolcall(text):
    s = re.sub(r"```tool_call.*?```", "", str(text or ""), flags=re.DOTALL)
    return re.sub(r'\{\s*"name"\s*:\s*"[^"]+"\s*(?:,\s*"parameters"\s*:\s*\{[^}]*\})?\s*\}', "", s)


def _digest(content, role):
    """把一条消息压成一行关键信息（纯抽取，不调用模型）"""
    s = str(content or "")
    if s.startswith(_TOOL_TAG):
        body = s[len(_TOOL_TAG):].lstrip()
        paths = _uniq(_PATH_RE.findall(body))[:3]
        errs = [e.strip() for e in _ERR_RE.findall(body)][:2]
        ok = not any(k in body[:200] for k in ("错误", "失败", "Error", "error", "Traceback"))
        parts = ["[工具结果]" + ("成功" if ok else "失败")]
        if paths:
            parts.append("文件:" + ";".join(p[:60] for p in paths))
        if errs:
            parts.append("报错:" + " / ".join(e[:70] for e in errs))
        if not paths and not errs:
            parts.append(_one_line(body, 90))
        return "；".join(parts)[:260]
    tag = {"user": "[用户]", "assistant": "[助手]"}.get(role, "[%s]" % role)
    m = re.search(r'"name"\s*:\s*"([^"]+)"', s)
    if role == "assistant" and m:
        return (tag + " 调用工具 " + m.group(1))[:260]
    body = _strip_toolcall(s)
    if not body.strip():
        return ""
    return (tag + " " + _one_line(body, 150))[:260]


def shrink_tool_payload(text, limit=TOOL_KEEP_CHARS):
    """工具结果注入前降采样：保留前 2/3 + 后 1/3，中间标注省略"""
    s = str(text or "")
    if len(s) <= limit:
        return s
    head = int(limit * 0.66)
    tail = limit - head
    return (s[:head]
            + "\n…（此处省略 %d 字，完整结果已在界面展示）…\n" % (len(s) - limit)
            + s[-tail:])


# ---------------- L5 外部记忆（落盘流水账 + 按话题召回） ----------------
def _journal_path(sid):
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", str(sid or "default"))[:60] or "default"
    return JOURNAL_DIR / (safe + ".jsonl")


def journal_append(sid, role, text):
    """把被池化挤出的原文追加到会话流水账（永久保留，不受上下文窗口限制）"""
    s = str(text or "").strip()
    if not s or s.startswith(_TOOL_TAG):
        return                      # 工具结果噪声大，不入流水账
    try:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        p = _journal_path(sid)
        if p.exists() and p.stat().st_size > JOURNAL_MAX_BYTES:
            lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
            p.write_text("\n".join(lines[len(lines) // 2:]) + "\n", encoding="utf-8")
        body = s[:JOURNAL_ENTRY_CHARS]
        sig = hashlib.md5((role + "|" + body).encode("utf-8")).hexdigest()[:16]
        # 去重：客户端每轮会重发全量历史，同一条会被反复挤出 → 最近 80 条内已存在则跳过
        if p.exists():
            if any(sig in ln for ln in
                   p.read_text(encoding="utf-8", errors="ignore").splitlines()[-80:]):
                return
        rec = {"ts": int(time.time()), "role": role, "text": body, "sig": sig}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _terms(text):
    """查询关键词：英文词 + 中文二元字组"""
    out = set()
    for w in _TERM_RE.findall(str(text or "").lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", w):
            out.update(w[i:i + 2] for i in range(len(w) - 1))
            if len(w) >= 4:
                out.add(w)
        else:
            out.add(w)
    return out


def recall_journal(sid, query, top=RECALL_TOP, budget_tokens=RECALL_MAX_TOKENS, exclude=""):
    """从流水账里按关键词相关性召回较早过程，生成注入系统提示词的小块
    exclude：仍在窗口里的近期原文，命中则跳过（避免重复占用预算）"""
    qs = _terms(query)
    if not qs:
        return ""
    p = _journal_path(sid)
    if not p.exists():
        return ""
    try:
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()[-JOURNAL_TAIL_LINES:]
    except Exception:
        return ""
    total = max(1, len(lines))
    scored = []
    for idx, ln in enumerate(lines):
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        body = str(rec.get("text", "")).lower()
        hits = sum(1 for t in qs if t in body)
        if hits <= 0:
            continue
        # 纯相关性打分；同分时先到先得（越早的过程越可能是关键决策）
        scored.append((float(hits), rec))
    if not scored:
        return ""
    scored.sort(key=lambda x: -x[0])
    picked, used, seen = [], 0, set()
    for _sc, rec in scored:
        raw = str(rec.get("text", ""))
        if exclude and raw[:40] in exclude:
            continue            # 这条还在当前窗口里，无需召回
        text = _one_line(raw, 160)
        if text in seen:
            continue
        seen.add(text)
        line = "- [%s] %s" % ("用户" if rec.get("role") == "user" else "助手", text)
        cost = estimate_tokens(line)
        if used + cost > budget_tokens:
            break
        picked.append(line)
        used += cost
        if len(picked) >= top:
            break
    if not picked:
        return ""
    return RECALL_MARK + "\n" + "\n".join(picked)


def journal_info(sid):
    p = _journal_path(sid)
    try:
        if not p.exists():
            return {"entries": 0, "bytes": 0}
        return {"entries": len(p.read_text(encoding="utf-8", errors="ignore").splitlines()),
                "bytes": p.stat().st_size}
    except Exception:
        return {"entries": 0, "bytes": 0}


# ---------------- 折叠对话：落盘 / 恢复 / 结构化摘要 ----------------
def _fold_path(sid):
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", str(sid or "default"))[:60] or "default"
    return FOLD_DIR / (safe + ".json")


def _msg_sig(role, content):
    """与流水账同一套签名：按出现次数逐条消费，避免同文重复时误删最新消息"""
    body = str(content or "")[:JOURNAL_ENTRY_CHARS]
    return hashlib.md5((str(role) + "|" + body).encode("utf-8")).hexdigest()[:16]


def _fold_save(sid, st):
    try:
        FOLD_DIR.mkdir(parents=True, exist_ok=True)
        p = _fold_path(sid)
        data = {"sid": sid, "updated": int(time.time()), "folds": st.get("folds") or []}
        tmp = str(p) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, str(p))
    except Exception:
        pass


def _fold_load(sid, st):
    """重启后恢复：折叠记录 + 对应历史摘要块（模型侧仍然只看到摘要）"""
    if st.get("folds_loaded"):
        return
    st["folds_loaded"] = True
    p = _fold_path(sid)
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        return
    st["folds"] = list(data.get("folds") or [])
    if st["folds"] and not st["hist"]:
        for rec in st["folds"]:
            for e in (rec.get("hist_entries") or []):
                if e:
                    st["hist"].append({"text": str(e), "tokens": estimate_tokens(e)})
        _pool_hist(st)
        st["recall_dirty"] = True


def _file_hits(text):
    out = []
    for m in _FILE_EXT_RE.finditer(str(text or "")):
        p = m.group(0).strip("`\"'.,;:()[]{}").replace("\\\\", "\\")
        if len(p) < 3 or p.lower().startswith(("http", "www.")) or "://" in p:
            continue
        ctx = text[max(0, m.start() - 24):m.start()]
        act = "新增" if _ACT_NEW_RE.search(ctx) else ("修改" if _ACT_MOD_RE.search(ctx) else "涉及")
        out.append({"path": p[:160], "action": act})
    return out


def _dedup_files(files):
    d = {}
    for f in files:
        p = f.get("path")
        if not p:
            continue
        if p not in d or d[p] == "涉及":
            d[p] = f.get("action") or "涉及"
    return [{"path": p, "action": a} for p, a in d.items()][:10]


def _extract_lines(text, regex, limit=6, width=160):
    out = []
    for m in regex.finditer(text or ""):
        s = _one_line(m.group(0), width)
        if s and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _build_fold_summary(turns):
    """折叠产物：已确认的决定 / 改动过的文件 / 未完成事项 / 关键结论"""
    decisions, files, pending, conclusions = [], [], [], []
    for t in turns:
        last_assistant = ""
        for role, text in t.get("msgs") or []:
            raw = _strip_toolcall(text)
            if raw.startswith(_TOOL_TAG):
                body = raw[len(_TOOL_TAG):]
                for e in _ERR_RE.findall(body)[:1]:
                    pending.append("工具失败：" + _one_line(e, 90))
                files.extend(_file_hits(body))
                continue
            for d in _extract_lines(raw, _DECISION_RE, 3):
                # 「关键结论：…」归到结论，不重复塞进决定
                (conclusions if "关键结论" in d else decisions).append(d)
            pending.extend(_extract_lines(raw, _TODO_RE, 3))
            files.extend(_file_hits(raw))
            if role == "assistant":
                last_assistant = raw
        if last_assistant.strip():
            conclusions.append(_one_line(last_assistant, 120))
    return {
        "decisions": _uniq(decisions)[:6],
        "files": _dedup_files(files),
        "pending": _uniq(pending)[:6],
        "conclusions": _uniq(conclusions)[-4:],
    }


def _summary_hist_text(summary, turn_count):
    parts = []
    if summary.get("decisions"):
        parts.append("已确认决定：" + "；".join(x[:80] for x in summary["decisions"][:3]))
    if summary.get("files"):
        parts.append("改动文件：" + "；".join(
            (f.get("path", "") + "(" + f.get("action", "涉及") + ")")[:70]
            for f in summary["files"][:4]))
    if summary.get("pending"):
        parts.append("未完成：" + "；".join(x[:80] for x in summary["pending"][:3]))
    if summary.get("conclusions"):
        parts.append("关键结论：" + "；".join(x[:80] for x in summary["conclusions"][-2:]))
    head = "[折叠摘要·%d轮] " % max(1, turn_count)
    return _one_line(head + " | ".join(parts), 700)


def apply_folds(sid, messages):
    """把已折叠的原文从本轮模型上下文中移除（界面展开不影响这里）。返回移除条数。"""
    with _LOCK:
        st = _state(sid)
        _fold_load(sid, st)
        if not messages:
            return 0
        offset = 1 if messages[0].get("role") == "system" else 0
        body = list(messages[offset:])
        queue = {}
        for rec in st.get("folds") or []:
            for it in rec.get("items") or []:
                s = it.get("sig")
                if s:
                    queue.setdefault(s, []).append(it)
        kept = []
        for m in body:
            s = _msg_sig(m.get("role"), m.get("content"))
            if s in queue and queue[s]:
                queue[s].pop(0)      # 逐条消费：只移除确实被折叠过的那一条
                continue
            kept.append(m)
        removed = len(body) - len(kept)
        if removed:
            messages[:] = ([messages[0]] if offset else []) + kept
        return removed


def fold_state(sid):
    """折叠状态（供面板渲染折叠条 / 重启后恢复）"""
    with _LOCK:
        st = _state(sid)
        _fold_load(sid, st)
        folds = st.get("folds") or []
        return {
            "sid": sid,
            "thresholds": {"warn": FOLD_WARN_RATIO, "hard": FOLD_HARD_RATIO,
                           "soft_target": FOLD_SOFT_TARGET, "hard_target": FOLD_HARD_TARGET},
            "folded_through": max([int(r.get("covered_after") or -1) for r in folds] + [-1]),
            "turn_count": sum(int(r.get("turn_count") or 0) for r in folds),
            "folds": folds,
            "journal": journal_info(sid),
        }


def reset_folds(sid):
    """手动 /compact 后旧折叠记录失效：只清 UI 折叠记录，历史摘要块保留在上下文里"""
    with _LOCK:
        st = _state(sid)
        st["folds_loaded"] = True
        st["folds"] = []
        try:
            p = _fold_path(sid)
            if p.exists():
                p.unlink()
        except Exception:
            pass


def _rounds_of(body, orig_idx):
    """按 user 消息切轮；返回 [{start_pos,end_pos,start_idx,end_idx,msgs}]"""
    rounds, cur = [], None
    for pos, (oi, m) in enumerate(zip(orig_idx, body)):
        role = str(m.get("role") or "")
        text = str(m.get("content") or "")
        is_user = role == "user" and not text.startswith(_TOOL_TAG)
        if is_user:
            cur = {"start_pos": pos, "end_pos": pos, "start_idx": oi, "end_idx": oi,
                   "msgs": [(role, text)]}
            rounds.append(cur)
        else:
            if cur is None:
                cur = {"start_pos": pos, "end_pos": pos, "start_idx": oi, "end_idx": oi,
                       "msgs": []}
                rounds.append(cur)
            cur["end_pos"] = pos
            cur["end_idx"] = oi
            cur["msgs"].append((role, text))
    return rounds


# 折叠统计：从被折叠的 assistant 消息里解析 ```tool_call {...}```，
# 供面板显示「已编辑 N 个文件，读取 N 个文件，搜索 N 次，执行 N 条命令」
_TOOLCALL_RE = re.compile(r"```tool(?:_call)?\s*\n?(\{[\s\S]*?\})\s*```")
_FOLD_KIND = {
    "file_write": "edit", "apply_patch": "edit", "file_copy": "edit",
    "file_move": "edit", "file_delete": "edit", "edit_file": "edit",
    "write_file": "edit", "ide_edit_file": "edit",
    "file_read": "read", "code_read": "read", "read_file": "read",
    "read_files": "read", "file_read_many": "read",
    "file_search": "search", "code_search": "search", "grep": "search",
    "search_files": "search",
    "file_list": "list", "list_files": "list",
    "shell_exec": "cmd", "run_python": "cmd", "run_command": "cmd",
    "execute_command": "cmd", "process_start": "cmd",
}


def _fold_stats(msgs):
    """统计被折叠消息里的工具动作数量（只数调用，不数结果）"""
    cnt = {"edit": 0, "read": 0, "search": 0, "list": 0, "cmd": 0}
    for m in msgs or []:
        if m.get("role") != "assistant":
            continue
        for mt in _TOOLCALL_RE.finditer(str(m.get("content") or "")):
            try:
                obj = json.loads(mt.group(1))
            except Exception:
                continue
            name = str((obj or {}).get("name") or (obj or {}).get("tool") or "").strip()
            kind = _FOLD_KIND.get(name)
            if kind:
                cnt[kind] += 1
    return cnt


def fold_session(sid, messages, n_ctx=8192, max_tokens=0, force=False,
                 warn_ratio=None, hard_ratio=None):
    """折叠对话主入口：按窗口占用分级，把最早若干轮折叠成结构化摘要。

    复用既有流水线：journal_append（L5 落盘）、_add_hist/_pool_hist（L3 历史块）、
    _digest（抽取式要点）、_render（拼回系统提示）。新增：按轮折叠、结构化摘要、
    原始轮次溯源、折叠记录落盘/恢复、apply_folds（模型侧只保留摘要）。
    """
    with _LOCK:
        st = _state(sid)
        _fold_load(sid, st)
        if not messages:
            return None
        n_ctx = int(n_ctx or 8192)
        offset = 1 if messages[0].get("role") == "system" else 0
        if messages and messages[0].get("role") == "system" and not st.get("base"):
            st["base"] = _strip_dynamic(messages[0].get("content") or "")

        # 1) 已折叠原文先移出本轮模型上下文（即使不触发新折叠也必须做）
        body = list(messages[offset:])
        orig_idx = list(range(len(body)))
        queue = {}
        for rec in st.get("folds") or []:
            for it in rec.get("items") or []:
                s = it.get("sig")
                if s:
                    queue.setdefault(s, []).append(it)
        kept, keep_idx = [], []
        for j, m in enumerate(body):
            s = _msg_sig(m.get("role"), m.get("content"))
            if s in queue and queue[s]:
                queue[s].pop(0)
                continue
            kept.append(m)
            keep_idx.append(orig_idx[j])
        removed_existing = len(body) - len(kept)
        body, orig_idx = kept, keep_idx
        if removed_existing:
            messages[:] = ([messages[0]] if offset else []) + body
        # 关键：即使本轮不触发新折叠，也要把已恢复/已有的折叠摘要写回系统提示，
        # 否则重启后会出现「原文被移除、摘要却没注入」的空窗。
        _render(messages, st)

        est = estimate_messages(messages)
        ratio = est * 1.0 / max(1, n_ctx)
        _warn = warn_ratio if warn_ratio is not None else FOLD_WARN_RATIO
        _hard = hard_ratio if hard_ratio is not None else FOLD_HARD_RATIO
        if not force and ratio < _warn:
            return None
        tier = "manual" if force else ("hard" if ratio >= _hard else "soft")
        target = int(n_ctx * (FOLD_HARD_TARGET if tier in ("hard", "manual") else FOLD_SOFT_TARGET))

        rounds = _rounds_of(body, orig_idx)
        candidates = rounds[:-FOLD_KEEP_ROUNDS] if len(rounds) > FOLD_KEEP_ROUNDS else []
        # 已进入硬阈值时，允许只折 1 轮（否则会退化成 L4 直接截断）
        min_rounds = 1 if (force or ratio >= _hard) else FOLD_MIN_ROUNDS
        if len(candidates) < min_rounds:
            return None

        # 摘要会写回历史块（约 700 字，一次性开销），选轮时先预留出来
        selected, sim = [], est + 650
        for r in candidates:
            if len(selected) >= min_rounds and sim <= target:
                break
            selected.append(r)
            for _role, _text in r["msgs"]:
                sim -= estimate_tokens(_text) + 4
        if len(selected) < min_rounds:
            return None
        # 折叠同样有成本（摘要要写回历史块）。折掉的原文还抵不过摘要开销时就不折，
        # 否则会把上下文越折越大（大系统提示词场景尤其明显）。
        folded_tokens = sum(estimate_tokens(t) + 4
                            for r in selected for _role, t in r["msgs"])
        if not force and folded_tokens < 700:
            return None

        first_pos = selected[0]["start_pos"]
        last_pos = selected[-1]["end_pos"]
        folded = body[first_pos:last_pos + 1]
        folded_idx = orig_idx[first_pos:last_pos + 1]

        # 2) 结构化摘要 + 可追溯轮次
        turns = []
        for n, r in enumerate(selected, 1):
            user_text = ""
            assistant_text = ""
            for role, text in r["msgs"]:
                clean = _strip_toolcall(text)
                if role == "user" and not user_text and not clean.startswith(_TOOL_TAG):
                    user_text = clean
                if role == "assistant" and clean.strip():
                    assistant_text = clean
            turns.append({
                "n": n,
                "start": r["start_idx"],
                "end": r["end_idx"],
                "title": _one_line(user_text, 44) or ("第 %d 轮" % n),
                "user": _one_line(user_text, 220),
                "assistant": _one_line(assistant_text, 220),
                "ts": int(time.time()),
                "msgs": r["msgs"],
            })
        summary = _build_fold_summary(turns)
        hist_text = _summary_hist_text(summary, len(turns))
        # 模型侧只放一段结构化摘要；逐轮原文/预览只留在折叠记录里供界面展开
        hist_entries = [hist_text]

        # 3) 复用 L5 落盘 + L3 历史块；_pool_hist 会自动合并超限条目
        for m in folded:
            _remember_goal(st, m)
            journal_append(sid, m.get("role"), m.get("content"))
        for e in hist_entries:
            _add_hist(st, e)
        st["recall_dirty"] = True
        _render(messages, st)

        # 4) 从本轮上下文移除折叠轮（模型侧只保留摘要）
        del messages[offset + first_pos:offset + last_pos + 1]
        _render(messages, st)

        items = []
        for i, idx in enumerate(folded_idx):
            m = folded[i]
            items.append({
                "i": idx,
                "role": m.get("role"),
                "content": str(m.get("content") or "")[:FOLD_ITEM_CHARS],
                "sig": _msg_sig(m.get("role"), m.get("content")),
            })
        if len(items) > FOLD_MAX_ITEMS:
            half = FOLD_MAX_ITEMS // 2
            items = items[:half] + items[-half:]

        prev_covered = max([int(r.get("covered_after") or -1)
                            for r in (st.get("folds") or [])] + [-1])
        rec = {
            "id": "fold_%s_%s" % (time.strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:6]),
            "sid": sid,
            "created": int(time.time()),
            "tier": tier,
            "n_ctx": n_ctx,
            "before_tokens": est,
            "after_tokens": estimate_messages(messages),
            "usage_pct_before": round(ratio * 100.0, 1),
            "usage_pct_after": round(estimate_messages(messages) * 100.0 / max(1, n_ctx), 1),
            "turn_count": len(turns),
            "range": [int(folded_idx[0]), int(folded_idx[-1])],
            "covered_before": int(prev_covered),
            "covered_after": int(folded_idx[-1]),
            "summary": summary,
            "stats": _fold_stats(folded),
            "turns": [{k: v for k, v in t.items() if k != "msgs"} for t in turns],
            "hist_text": hist_text,
            "hist_entries": hist_entries,
            "items": items,
            "applied_existing": removed_existing,
            "thresholds": {"warn": _warn, "hard": _hard},
        }
        st["folds"] = list(st.get("folds") or []) + [rec]
        _fold_save(sid, st)
        return rec


# ---------------- 会话状态 ----------------
def _state(sid):
    st = _SESS.get(sid)
    if st is None:
        st = {"base": "", "goal": "", "hist": [], "stats": {},
              "recall": "", "recall_dirty": True,
              "folds": [], "folds_loaded": False}
        _SESS[sid] = st
    return st


def _remember_goal(st, msg):
    """把任务的第一条用户诉求钉住：无论压多少轮，模型都知道自己在干什么"""
    if st.get("goal"):
        return
    if msg.get("role") != "user":
        return
    text = _strip_toolcall(msg.get("content"))
    if not text.strip() or str(msg.get("content") or "").startswith(_TOOL_TAG):
        return
    st["goal"] = _one_line(text, GOAL_MAX_CHARS)


def set_system(sid, content):
    """记录系统提示词基座；内容里若已拼接折叠摘要/历史块/召回块则自动剥离"""
    with _LOCK:
        _state(sid)["base"] = _strip_dynamic(content)


def _is_fold_hist(h):
    """是否为折叠摘要条目（这类条目不参与合并/裁剪，否则摘要会丢）"""
    return str((h or {}).get("text") or "").startswith(FOLD_HIST_PREFIX)


def _strip_dynamic(content):
    """从系统提示词里剥掉本模块动态注入的块（折叠摘要头 / 历史块 / 召回块），只留基座"""
    s = str(content or "")
    if FOLD_HEAD_BEGIN in s:
        pre, rest = s.split(FOLD_HEAD_BEGIN, 1)
        if FOLD_HEAD_END in rest:
            rest = rest.split(FOLD_HEAD_END, 1)[1]
        s = pre + rest
    for mark in (HISTORY_MARK, RECALL_MARK):
        if mark in s:
            s = s.split(mark, 1)[0]
    return s.rstrip()


def _hist_block(st, include_folds=True):
    hist = [h for h in st["hist"]
            if include_folds or not _is_fold_hist(h)]
    goal = st.get("goal") or ""
    if not hist and not goal:
        return ""
    lines = [HISTORY_MARK.strip()]
    if goal:
        lines.append("【任务目标（原文钉住，勿丢失）】" + goal)
    for i, h in enumerate(hist, 1):
        lines.append("%d. %s" % (i, h["text"]))
    return "\n".join(lines)


def _render(messages, st):
    if messages and messages[0].get("role") == "system":
        parts = []
        # 折叠摘要放【头部】并加标记：即使下游超窗裁剪只保留系统提示前段，
        # 摘要也能活下来；正文/工具提示被裁不影响「历史要点进模型」。
        folds = [h["text"] for h in st["hist"] if _is_fold_hist(h)]
        if folds:
            parts.append(FOLD_HEAD_BEGIN + "\n" + FOLD_HEAD_MARK.strip()
                         + "\n" + "\n".join(folds) + "\n" + FOLD_HEAD_END)
        parts.append(st["base"])
        block = _hist_block(st, include_folds=False)
        if block:
            parts.append(block)
        rec = st.get("recall") or ""
        if rec:
            parts.append(rec)      # 长期记忆召回：补回被池化掉的细节
        messages[0]["content"] = "\n".join(p for p in parts if p)


def _refresh_recall(sid, st, messages):
    """按当前话题刷新长期记忆召回块；只在历史刚变化时重算（避免每轮改动系统提示词打碎 KV 缓存）"""
    if not st.get("recall_dirty"):
        return
    st["recall_dirty"] = False
    if not st["hist"]:
        st["recall"] = ""       # 还没发生过池化，无召回可言
        return
    q = ""
    for m in reversed(messages):
        c = str(m.get("content") or "")
        if m.get("role") == "user" and not c.startswith(_TOOL_TAG):
            q = c
            break
    # 仍在窗口里的近期原文，召回时跳过
    recent = "\n".join(str(m.get("content") or "") for m in messages[-8:])
    st["recall"] = recall_journal(sid, q, exclude=recent) if q else ""


def _add_hist(st, text):
    if not text:
        return
    if any(h["text"] == text for h in st["hist"][-4:]):
        return   # 同一要点反复出现（工具重试/复读）→ 不重复占位
    st["hist"].append({"text": text, "tokens": estimate_tokens(text)})
    st["recall_dirty"] = True    # 历史有变化 → 下次 fit 重新计算召回块
    _pool_hist(st)


def _pool_hist(st):
    """L3：历史块超限 → 最旧两条合并（感受野扩大），并限制条目数。
    折叠摘要条目受保护：不参与合并/截断，否则模型侧的历史要点会被抹掉。"""
    hist = st["hist"]
    guard = 0
    while hist and (sum(h["tokens"] for h in hist) > HISTORY_MAX_TOKENS
                    or len(hist) > HISTORY_MAX_ITEMS):
        guard += 1
        if guard > 200:
            break
        idx = [i for i, h in enumerate(hist) if not _is_fold_hist(h)]
        if not idx:
            # 全是折叠摘要（极少见）：只压缩最长的一条，保留其前缀标记
            j = max(range(len(hist)), key=lambda i: hist[i]["tokens"])
            hist[j]["text"] = _one_line(hist[j]["text"], 450)
            hist[j]["tokens"] = estimate_tokens(hist[j]["text"])
            break
        if len(idx) == 1:
            i = idx[0]
            t = _one_line(hist[i]["text"], 400)
            if t == hist[i]["text"]:
                break        # 已压到最短，仍超限只能保留（不再空转）
            hist[i]["text"] = t
            hist[i]["tokens"] = estimate_tokens(t)
            continue
        a = hist.pop(idx[0])
        b = hist.pop(idx[1] - 1)     # 删掉第一条后，第二条下标前移 1
        ta = re.sub(r"^（合并）\s*", "", a["text"]).strip()
        tb = re.sub(r"^（合并）\s*", "", b["text"]).strip()
        merged = ("（合并）" + _one_line(ta, 120) + " ｜ " + _one_line(tb, 120))[:300]
        hist.insert(idx[0], {"text": merged, "tokens": estimate_tokens(merged)})


def _drop_oldest_hist(st):
    """预算不够时让位：从最旧的历史条目删一条，但折叠摘要永不删除。
    返回是否真的删掉了（False = 只剩受保护条目，不再删）。"""
    hist = st["hist"]
    for i, h in enumerate(hist):
        if _is_fold_hist(h):
            continue
        hist.pop(i)
        return True
    return False


# ---------------- 主入口 ----------------
def fit(sid, messages, budget, keep_recent=6):
    """原地压缩 messages，使其估算 token ≤ budget；返回被移出队列的原始消息"""
    dropped = []
    if not messages:
        return dropped
    with _LOCK:
        st = _state(sid)
        if messages[0].get("role") == "system":
            # 剥掉折叠摘要头/历史块/召回块，只留基座（否则会把动态块当基座累积）
            head = _strip_dynamic(messages[0].get("content", ""))
            # 只认「不含动态块」的内容为新基座：外层注入新系统提示（如 IDE 上下文）时更新，
            # 否则沿用已有基座，避免把历史块反复拼进系统提示导致无限膨胀
            if head and head != st["base"] and not (st["base"] and st["base"] in head):
                st["base"] = head
        _refresh_recall(sid, st, messages)
        _render(messages, st)
        if estimate_messages(messages) <= budget:
            st["stats"].update(_measure(messages, st, budget))
            return dropped

        protect = max(2, int(keep_recent))

        # L1 局部池化：较早的大块工具结果首尾降采样
        body = messages[1:]
        older = body[:-protect] if len(body) > protect else []
        for m in older:
            c = str(m.get("content") or "")
            if c.startswith(_TOOL_TAG) and len(c) > 900:
                m["content"] = _TOOL_TAG + "\n" + shrink_tool_payload(c[len(_TOOL_TAG):], 800)
        _render(messages, st)
        if estimate_messages(messages) <= budget:
            st["stats"].update(_measure(messages, st, budget))
            return dropped

        # L2 区域池化：从最旧开始把整条消息抽成一行，移出队列写入历史块
        while len(messages) > protect + 1 and estimate_messages(messages) > budget:
            m = messages.pop(1)
            dropped.append(m)
            _remember_goal(st, m)
            journal_append(sid, m.get("role"), m.get("content"))   # L5 落盘，细节永不丢失
            _add_hist(st, _digest(m.get("content"), m.get("role")))
            _render(messages, st)

        _render(messages, st)
        if estimate_messages(messages) <= budget:
            st["stats"].update(_measure(messages, st, budget))
            return dropped

        # L4 硬裁剪：先丢最旧（保留 system + 最近若干条），再把末条逐步压到能装下
        while len(messages) > 2 and estimate_messages(messages) > budget:
            m = messages.pop(1)
            dropped.append(m)
            _remember_goal(st, m)
            journal_append(sid, m.get("role"), m.get("content"))   # L5 落盘，细节永不丢失
            _add_hist(st, _digest(m.get("content"), m.get("role")))
            _render(messages, st)

        last = messages[-1]
        # 当前用户消息不能被中间省略：里面可能带着「附件：<路径>（类型，大小）」引用，
        # 一旦从中间截断会把路径切断（附件识别失败），也会割裂用户的问题。
        # 用户问题原样保留，先让历史块让位；只有工具结果类末条才做首尾降采样。
        _last_is_user = (str(last.get("role") or "") == "user"
                         and not str(last.get("content") or "").startswith(_TOOL_TAG))
        if _last_is_user:
            while estimate_messages(messages) > budget and _drop_oldest_hist(st):
                _render(messages, st)
        guard = 0
        while (not _last_is_user) and estimate_messages(messages) > budget and guard < 16:
            guard += 1
            cur = str(last.get("content") or "")
            if len(cur) <= 200:
                break
            over = estimate_messages(messages) - budget
            target = max(200, len(cur) - max(over, 1) * 2)
            if target >= len(cur):
                target = len(cur) // 2
            last["content"] = shrink_tool_payload(cur, target)
            _render(messages, st)

        # 兜底：system + 历史块本身就撑满预算时，从最旧的历史条目开始让位
        while estimate_messages(messages) > budget and _drop_oldest_hist(st):
            _render(messages, st)

        st["stats"].update(_measure(messages, st, budget))
        return dropped


def _measure(messages, st, budget):
    used = estimate_messages(messages)
    return {"messages_tokens": used, "budget": budget,
            "used_pct": round(used * 100.0 / budget, 1) if budget else 0,
            "history_items": len(st["hist"]),
            "history_tokens": sum(h["tokens"] for h in st["hist"])}


def history_block(sid):
    with _LOCK:
        return _hist_block(_state(sid))


def compact(sid, messages=None, keep_items=4):
    """手动压缩（/compact）：把窗口外的较早消息折叠为历史要点并落盘流水账。
    messages 传入时原地裁剪（system + 最近 keep_items 条），返回压缩前后 token 与历史摘要。"""
    keep = max(2, int(keep_items or 4))
    with _LOCK:
        st = _state(sid)
        before = estimate_messages(messages) if messages else 0
        folded = 0
        folded_stats = {"edit": 0, "read": 0, "search": 0, "list": 0, "cmd": 0}
        if messages:
            head = messages[0] if messages[0].get("role") == "system" else None
            body = messages[1:] if head else messages[:]
            if len(body) > keep:
                folded_stats = _fold_stats(body[:-keep])
                for m in body[:-keep]:
                    _remember_goal(st, m)
                    journal_append(sid, m.get("role"), m.get("content"))
                    dg = _digest(m.get("content"), m.get("role"))
                    if dg:
                        _add_hist(st, dg)
                    folded += 1
                messages[:] = ([head] if head else []) + body[-keep:]
            st["recall_dirty"] = True
        after = estimate_messages(messages) if messages else 0
        summary = _hist_block(st).replace(HISTORY_MARK.strip(), "").strip()
        st["stats"].update({"messages_tokens": after})
        return {"sid": sid, "folded": folded, "keep_items": keep,
                "before_tokens": before, "after_tokens": after,
                "stats": folded_stats,
                "history_items": len(st["hist"]),
                "history_tokens": sum(h["tokens"] for h in st["hist"]),
                "summary": summary,
                "journal": journal_info(sid)}


def stats(sid):
    with _LOCK:
        st = _state(sid)
        return {"session": sid, "base_tokens": estimate_tokens(st["base"]),
                "history_items": len(st["hist"]),
                "history_tokens": sum(h["tokens"] for h in st["hist"]),
                "history": [h["text"] for h in st["hist"]][-8:],
                "recall_tokens": estimate_tokens(st.get("recall") or ""),
                "journal": journal_info(sid),
                **st["stats"]}


def reset(sid):
    with _LOCK:
        _SESS.pop(sid, None)

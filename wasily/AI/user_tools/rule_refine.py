# -*- coding: utf-8 -*-
"""提炼确定性路由规则（第四层自我进化）。

输入：wasily_log/web_run_hits.jsonl（每次 route 命中时追加一条）
输出：AI/web_run_rules.json（启动时 / 提炼后热加载）
机制：先用纯规则提炼（零风险、100% 可控），样本够多后可升级模型辅助。
"""

import json
import os
import re
import shutil
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_AI = os.path.dirname(_HERE)
HITS_FP = os.path.join(_AI, "wasily_log", "web_run_hits.jsonl")
RULES_FP = os.path.join(_AI, "web_run_rules.json")
RELOAD_URL = "http://127.0.0.1:8000/v1/web_rules/reload"

_DEV_HINTS = re.compile(
    # 纯开发/修改动词，避免误伤"运行项目里的网页"这类合法运行指令
    r"改造|重构|重写|改成|优化|美化|新增|添加|实现|开发|编写|修复|"
    r"设计成|加个|加一个|加功能|补.*函数|样式|布局", re.IGNORECASE)

# route 成功但被 route 拦截的语义（这些词本身不该触发 run_web_page，交给模型）
_FALSE_POS_RE = re.compile(
    r"运行(的时候|时)|打开(的时候|时)|"
    r"我(想要|想|要|希望).*(运行|打开)|"
    r"(运行|打开).*(会|应该|需要|可以|能)", re.IGNORECASE)


def _read_hits():
    if not os.path.isfile(HITS_FP):
        return []
    out = []
    try:
        with open(HITS_FP, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _load_existing():
    if not os.path.isfile(RULES_FP):
        return {"verbs": [], "objects": [], "blacklist": [], "note": ""}
    try:
        with open(RULES_FP, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"verbs": [], "objects": [], "blacklist": [], "note": ""}


def _backup_old():
    if not os.path.isfile(RULES_FP):
        return ""
    try:
        bak = RULES_FP + ".bak"
        shutil.copy2(RULES_FP, bak)
        return bak
    except Exception:
        return ""


def _try_reload():
    """调 /v1/web_rules/reload 热加载规则；后端未启动则跳过。"""
    try:
        req = urllib.request.Request(RELOAD_URL, data=b"{}",
                                      headers={"Content-Type": "application/json"},
                                      method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except Exception:
        return None


def _dedup_keep_order(items):
    seen = set(); out = []
    for x in items:
        x = str(x).strip()
        if not x or x in seen:
            continue
        seen.add(x); out.append(x)
    return out


def run():
    hits = _read_hits()
    total = len(hits)

    if total < 10:
        return {
            "ok": True, "refined": False,
            "message": ("路由命中样本只有 %d 条，不足以提炼出有意义的规则。"
                        "先多用几次「运行/打开网页」让样本累积起来。") % total,
            "hits": total, "need": 10,
        }

    successes = [h for h in hits if h.get("opened")]
    failures = [h for h in hits if not h.get("opened")]

    existing = _load_existing()
    black = list(existing.get("blacklist") or [])
    verbs = list(existing.get("verbs") or [])
    objects = list(existing.get("objects") or [])

    # ---- 失败样本提炼：开发意图被 route 接了 → 加黑名单 ----
    dev_success = [h for h in successes if _DEV_HINTS.search(h.get("q", ""))]
    for h in dev_success:
        q = str(h.get("q", ""))
        # 提取关键短语，生成一个保守正则（锚定最独特的 3-8 个汉字串）
        for pat in (
            r"把" + re.escape(q.split("把", 1)[-1][:12]) + r"",
            r"实现" + re.escape(q.split("实现", 1)[-1][:12]) + r"",
            r"开发.*网页",
            r"改造.*网页",
            r"网页.*改造",
            r"网页.*开发",
            r"网页.*实现",
        ):
            if pat not in black:
                black.append(pat)

    # ---- 失败样本提炼：opened=false 但目标为空 / err 不明 → route 误接了开发类 ----
    for h in failures:
        q = str(h.get("q", ""))
        err = str(h.get("err", ""))
        # 明显是开发意图但 route 接了 → 黑名单
        if _DEV_HINTS.search(q) and ("文件不存在" in err or not err):
            for pat in (r"网页.*(改造|开发|实现|修复)", r"(改造|开发|实现|修复).*网页"):
                if pat not in black:
                    black.append(pat)

    # ---- 保守：删掉太短/太泛的黑名单（避免误伤） ----
    black = [p for p in black if len(p) >= 6 or (p.startswith("(?:") and len(p) >= 20)]
    black = _dedup_keep_order(black)

    if not black and not verbs and not objects:
        return {
            "ok": True, "refined": False,
            "message": "样本 %d 条，但未发现可提炼的新模式，现有规则已覆盖。" % total,
            "hits": total,
        }

    # ---- 写规则 ----
    new_rules = {
        "verbs": _dedup_keep_order(verbs),
        "objects": _dedup_keep_order(objects),
        "blacklist": black,
        "note": ("rule_refine 自动提炼 v1 · %s · hits=%d success=%d fail=%d"
                 % (time.strftime("%Y-%m-%d %H:%M:%S"), total, len(successes), len(failures))),
    }
    bak = _backup_old()
    try:
        with open(RULES_FP, "w", encoding="utf-8") as f:
            json.dump(new_rules, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return {"ok": False, "message": "写规则文件失败：%s" % e}

    reload_res = _try_reload()

    return {
        "ok": True, "refined": True,
        "hits": total, "success": len(successes), "fail": len(failures),
        "new_blacklist": black,
        "backed_up": bak,
        "reload": reload_res,
        "rule_file": RULES_FP,
        "message": ("已从 %d 条命中样本中提炼出 %d 条黑名单规则，"
                    "并 %s 热加载到后端（当前黑名单 pattern: %s）"
                    % (total, len(black),
                       "成功" if (reload_res and reload_res.get("ok")) else "后端未启动，下次启动自动加载",
                       (reload_res or {}).get("pattern", "")[:120])),
    }


SPEC = {
    "name": "rule_refine",
    "description": "从确定性路由的命中样本里提炼黑名单和同义词规则。用户说「提炼规则 / 学习路由」时调用，让 route 自动进化",
    "parameters": {},
}

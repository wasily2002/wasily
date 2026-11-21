# -*- coding: utf-8 -*-
"""技能系统：skills/*.md（JSON 头部 + 提示词正文），按触发词命中后注入系统提示词
文件格式：
---
{"name": "技能名", "triggers": ["触发词1", "触发词2"], "description": "一句话说明"}
---
技能提示词正文（命中后注入，指导模型本轮行为）
"""
import json
import re
import time
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent / "skills"
_WASILY_ROOT = Path(__file__).resolve().parent.parent
# 可派发 skill 的扫描范围：内置 → 用户级 .trae-cn → 项目内本地 skill 目录
SKILL_SCAN_DIRS = [
    ("builtin", SKILLS_DIR),
    ("user", Path.home() / ".trae-cn" / "skills"),
    ("user", Path.home() / ".trae" / "skills"),
    ("project", _WASILY_ROOT / "skills"),
    ("project", _WASILY_ROOT / ".trae" / "skills"),
    ("project", _WASILY_ROOT / ".trae-cn" / "skills"),
]
_CATALOG_CACHE = {"ts": 0.0, "data": []}


def _parse_frontmatter(text):
    """解析 --- 包裹的头部：支持 JSON 与简单 key: value 两种写法。"""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    raw, body = m.group(1).strip(), m.group(2)
    meta = {}
    if raw.startswith("{"):
        try:
            meta = json.loads(raw)
        except Exception:
            meta = {}
    if not meta:
        lines = raw.splitlines()
        i = 0
        while i < len(lines):
            mm = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", lines[i].strip())
            if not mm:
                i += 1
                continue
            k, v = mm.group(1), mm.group(2).strip()
            if v in (">", ">-", ">+", "|", "|-", "|+"):
                # YAML 块文本（常见于 description: >-）：收集后续缩进行
                j, parts = i + 1, []
                while j < len(lines) and (not lines[j].strip() or lines[j].startswith((" ", "\t"))):
                    if lines[j].strip():
                        parts.append(lines[j].strip())
                    j += 1
                meta[k] = " ".join(parts)
                i = j
                continue
            if v.startswith("[") and v.endswith("]"):
                try:
                    v = json.loads(v)
                except Exception:
                    v = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
            meta[k] = v
            i += 1
    return meta, body


def _derive_triggers(meta, body):
    trig = meta.get("triggers") or meta.get("trigger") or []
    if isinstance(trig, str):
        trig = re.split(r"[,，、;；\s]+", trig)
    trig = [str(t).strip() for t in trig if str(t).strip()]
    name = str(meta.get("name") or "").strip()
    if name:
        trig.append(name)
    # 从 description 派生触发词：中文短词直接收；英文只收 >=4 字且非停用词，避免「Use/the」乱命中
    _STOP = {"use", "used", "uses", "the", "and", "or", "for", "when", "this", "that",
             "with", "from", "your", "you", "user", "any", "all", "not", "are", "can",
             "will", "has", "have", "was", "were", "its", "into", "about", "should"}
    desc = str(meta.get("description") or "").strip()
    picked = 0
    for seg in re.split(r"[，,。；;、/（）()\s]+", desc):
        seg = seg.strip().strip(".:：-")
        if not seg or seg in trig:
            continue
        if re.fullmatch(r"[A-Za-z0-9_\-\.]+", seg):
            if len(seg) < 4 or seg.lower() in _STOP:
                continue
        elif not (2 <= len(seg) <= 10):
            continue
        trig.append(seg)
        picked += 1
        if picked >= 6:
            break
    # 去重保序
    out = []
    for t in trig:
        if t not in out:
            out.append(t)
    return out[:12]


def _load_skill_file(path, source):
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    meta, body = _parse_frontmatter(text)
    name = str(meta.get("name") or path.parent.name if path.name == "SKILL.md" else meta.get("name") or path.stem).strip()
    desc = str(meta.get("description") or "").strip()
    # Trae 的 SKILL.md 常在正文首行写标题/说明，description 缺失时兜底取首段
    if not desc:
        for line in body.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                desc = line[:160]
                break
    return {
        "name": name,
        "description": desc[:240],
        "triggers": _derive_triggers(meta, body),
        # 市场详情与显式运行需要完整技能正文；只在注入模型时按上下文预算截断。
        "prompt": body.strip(),
        "source": source,
        "file": str(path),
    }


def _catalog_with_state(data):
    """状态每次动态叠加，禁用/卸载后无需等待扫描缓存过期。"""
    try:
        import config as _cfg
        disabled = {str(x).strip().lower() for x in (_cfg.get("skills.disabled") or [])}
        uninstalled = {str(x).strip().lower() for x in (_cfg.get("skills.uninstalled") or [])}
    except Exception:
        disabled, uninstalled = set(), set()
    result = []
    for skill in data:
        item = dict(skill)
        key = item["name"].strip().lower()
        item["installed"] = key not in uninstalled
        item["enabled"] = item["installed"] and key not in disabled
        result.append(item)
    return result


def skill_catalog(force=False):
    """扫描本机 skill；市场返回全部项目，运行链路只使用已安装且启用的项目。"""
    now = time.time()
    if not force and _CATALOG_CACHE["data"] and (now - _CATALOG_CACHE["ts"]) < 60:
        return _catalog_with_state(_CATALOG_CACHE["data"])
    out = {}
    for source, d in SKILL_SCAN_DIRS:
        try:
            if not d.is_dir():
                continue
            files = list(d.glob("*.md")) + list(d.glob("*/SKILL.md")) + list(d.glob("*/skill.md"))
        except Exception:
            continue
        for f in files:
            s = _load_skill_file(f, source)
            if not s or not s["name"]:
                continue
            key = s["name"].strip().lower()
            # 后扫描的 project 级覆盖 user/builtin 同名技能
            out[key] = s
    data = sorted(out.values(), key=lambda s: (s["source"] != "project", s["name"]))
    _CATALOG_CACHE.update({"ts": now, "data": data})
    return _catalog_with_state(data)


def active_skill_catalog():
    return [s for s in skill_catalog() if s.get("installed") and s.get("enabled")]


def get_skill(name, include_inactive=False):
    """按显示名精确查询；详情可包含未安装项，执行默认只允许启用项。"""
    if not name:
        return None
    key = str(name).strip().lower()
    for skill in skill_catalog():
        if skill["name"].strip().lower() != key:
            continue
        if include_inactive or (skill.get("installed") and skill.get("enabled")):
            return skill
    return None


def set_skill_state(name, action):
    """市场操作只写状态，不删除本机原始 SKILL.md，卸载后始终可恢复。"""
    skill = get_skill(name, include_inactive=True)
    if not skill:
        return {"ok": False, "error": "未找到该技能"}
    action = str(action or "").strip().lower()
    if action not in ("install", "uninstall", "enable", "disable"):
        return {"ok": False, "error": "不支持的操作"}
    if action in ("enable", "disable") and not skill.get("installed"):
        return {"ok": False, "error": "请先安装该技能"}

    import config as _cfg
    key = skill["name"].strip().lower()
    disabled = {str(x).strip().lower() for x in (_cfg.get("skills.disabled") or [])}
    uninstalled = {str(x).strip().lower() for x in (_cfg.get("skills.uninstalled") or [])}
    if action == "install":
        uninstalled.discard(key)
        disabled.discard(key)
    elif action == "uninstall":
        uninstalled.add(key)
    elif action == "enable":
        disabled.discard(key)
    else:
        disabled.add(key)
    # 两项状态作为同一次用户操作落盘，避免只保存一半。
    _cfg.set_value("skills.disabled", sorted(disabled), save=False)
    _cfg.set_value("skills.uninstalled", sorted(uninstalled))
    current = get_skill(skill["name"], include_inactive=True)
    return {"ok": True, "skill": current,
            "message": {"install": "安装成功", "uninstall": "已卸载",
                        "enable": "已启用", "disable": "已禁用"}[action]}


def catalog_prompt(limit=48):
    """给规划模型的可派发技能清单（紧凑，避免占满上下文）。"""
    cat = active_skill_catalog()
    if not cat:
        return ""
    lines = ["", "可派发 skill 清单（步骤的 skill 字段从这里选；没有匹配就留空）："]
    for s in cat[:limit]:
        trig = "/".join(s["triggers"][:5])
        lines.append("- %s：%s%s" % (s["name"], s["description"][:90],
                                   ("（关键词：%s）" % trig) if trig else ""))
    return "\n".join(lines)


def find_skill(name):
    return get_skill(name)


def assign_skill(step):
    """按 名称/触发词 命中给步骤分配 skill；命中不了返回空（走通用工具执行，不硬凑）。"""
    if not step:
        return ""
    text = " ".join(str(step.get(k) or "") for k in ("title", "goal", "scope", "expected"))
    best, best_score = "", 0
    for s in active_skill_catalog():
        score = 0
        nm = s["name"].strip()
        if nm and nm.lower() in text.lower():
            score += 3
        for t in s["triggers"]:
            if len(t) >= 2 and t in text:
                score += 1
        if score > best_score:
            best, best_score = nm, score
    return best if best_score >= 1 else ""


def skill_prompt_for(name, limit=1800):
    s = find_skill(name)
    if not s:
        return ""
    return "\n\n## 本步骤技能：%s\n%s" % (s["name"], (s["prompt"] or "")[:limit])


def load_skills() -> list:
    SKILLS_DIR.mkdir(exist_ok=True)
    out = []
    for f in sorted(SKILLS_DIR.glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8")
            m = re.match(r"^---\s*\n(\{.*?\})\s*\n---\s*\n(.*)$", text, re.DOTALL)
            if not m:
                continue
            meta = json.loads(m.group(1))
            out.append({"name": meta.get("name", f.stem),
                        "triggers": meta.get("triggers", []),
                        "description": meta.get("description", ""),
                        "prompt": m.group(2).strip(),
                        "file": f.name})
        except Exception:
            continue
    return out


def match_skills(text: str) -> list:
    """仅匹配市场内已安装且启用的技能，大小写与扫描目录保持一致。"""
    if not text:
        return []
    folded = str(text).lower()
    return [s for s in active_skill_catalog()
            if any(t and str(t).lower() in folded for t in s["triggers"])]


def skills_prompt(matched: list) -> str:
    if not matched:
        return ""
    lines = ["", "## 命中技能（本轮必须严格遵循以下技能指引）"]
    for s in matched:
        lines.append(f"### 技能：{s['name']}\n{s['prompt']}")
    return "\n".join(lines)

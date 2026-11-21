# -*- coding: utf-8 -*-
"""REA（https://github.com/morluto/rea）逆向分析工具。

为什么包一层 CLI 而不是直接接 REA 的 MCP：REA 会暴露 255 个工具，
本地 7B 模型上下文装不下；这里只暴露少量动作，其余用 action=cli 兜底。
REA 要求 Node 22/24/26，本机 Node 25 会被拒绝，所以用 wasily/tools/rea-runtime 下的便携 Node 24。
"""

import json
import shlex
import subprocess
import time
from pathlib import Path

SPEC = {
    "name": "rea",
    "description": "REA 逆向分析：看本地软件/二进制/JS-Electron/.NET/APK/网页，返回结构、证据与限制",
    "parameters": {
        "target": "目标路径（文件或目录，绝对路径）或网址",
        "action": "auto/overview/javascript/audit/doctor/cli，默认 auto",
        "args": "action=cli 时填完整子命令，如 'decompile --help'",
        "timeout": "超时秒数，默认 300",
    },
}

_ROOT = Path(__file__).resolve().parents[2]          # wasily/
_RUNTIME = _ROOT / "tools" / "rea-runtime"           # 便携 Node 24 + rea-agents
_OUT_DIR = Path(__file__).resolve().parents[1] / "agent" / "outputs"


def _node_exe():
    """定位便携 Node 24（版本号可能随升级变化，按前缀找）"""
    for p in sorted(_RUNTIME.glob("node-v24*/node.exe")):
        if p.exists():
            return p
    return None


def _rea_script():
    p = _RUNTIME / "rea" / "node_modules" / "rea-agents" / "scripts" / "rea.mjs"
    return p if p.exists() else None


def _clip(text, limit=2400):
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n…（已截断）"


def _shape(v, depth=0):
    """把未知结构压成一行：只保留类型/长度/键名，避免把源码灌进上下文"""
    if isinstance(v, dict):
        keys = list(v)[:8]
        return "{" + ", ".join(keys) + ("…" if len(v) > 8 else "") + "}"
    if isinstance(v, list):
        return f"list({len(v)})"
    s = str(v).replace("\n", " ")
    return s[:100] + ("…" if len(s) > 100 else "")


def _summarize(data):
    """REA 的 JSON 可能几十 MB，只回摘要，细节让 agent 去读落盘文件"""
    if not isinstance(data, dict):
        return _clip(data, 1200)

    if data.get("error"):
        rem = data.get("remediation") or {}
        lines = [f"错误: {data.get('error')}",
                 f"code: {data.get('code', '')}"]
        if data.get("message"):
            lines.append(str(data["message"]))
        if isinstance(rem, dict) and rem.get("action"):
            lines.append(f"建议: {rem['action']}")
        details = data.get("details") or {}
        if isinstance(details, dict) and details.get("rejections"):
            ids = [r.get("provider_id") for r in details["rejections"] if isinstance(r, dict)]
            lines.append("不可用的分析引擎: " + ", ".join(str(i) for i in ids))
        return "\n".join(lines)

    lines = []
    # doctor：把检查项压成「可用 / 缺什么」，方便直接回答用户「现在能分析什么」
    if isinstance(data.get("checks"), list) and "healthy" in data:
        lines.append(f"健康: {data.get('healthy')}")
        bad = [c for c in data["checks"] if isinstance(c, dict) and not c.get("ok")]
        ok = [c.get("name") for c in data["checks"] if isinstance(c, dict) and c.get("ok")]
        if ok:
            lines.append("可用: " + ", ".join(str(n) for n in ok))
        for c in bad[:8]:
            # config_drift 是给各家 IDE 注册用的自检项，与本机分析能力无关，不报给用户
            if c.get("classification") == "config_drift":
                continue
            lines.append(f"缺失: {c.get('name')}（{c.get('classification', '')}）")
        return _clip("\n".join(lines))

    for k in ("operation", "predicate_type", "confidence", "authority"):
        if data.get(k):
            lines.append(f"{k}: {data[k]}")
    subj = data.get("subject")
    if isinstance(subj, dict):
        lines.append("subject: " + ", ".join(f"{k}={_shape(subj[k])}" for k in list(subj)[:4]))
    provider = data.get("provider")
    if isinstance(provider, dict):
        lines.append("provider: " + ", ".join(f"{k}={_shape(provider[k])}" for k in list(provider)[:3]))

    nr = data.get("normalized_result")
    if isinstance(nr, dict) and nr:
        lines.append("normalized_result:")
        for k, v in list(nr.items())[:14]:
            if isinstance(v, dict):
                lines.append(f"  - {k}: {_shape(v)}")
            elif isinstance(v, list):
                lines.append(f"  - {k}: list({len(v)})")
            else:
                lines.append(f"  - {k}: {_shape(v)}")

    lim = data.get("limitations")
    if isinstance(lim, list) and lim:
        lines.append("limitations:")
        for item in lim[:6]:
            lines.append("  - " + _shape(item))

    if not lines:
        # 不认识的返回结构：退化成顶层键概览
        for k, v in list(data.items())[:12]:
            lines.append(f"{k}: {_shape(v)}")
    return _clip("\n".join(lines))


def _pick_argv(action, target, args):
    """把动作映射成 REA 子命令"""
    action = (action or "auto").strip().lower()
    target = (target or "").strip().strip('"')
    if action == "cli":
        if not args:
            return ["--help"], ""
        parts = shlex.split(args)
        if "--format" not in parts:
            parts += ["--format", "json"]
        return parts, ""

    if action == "doctor":
        return ["doctor", "--format", "json"], ""
    if not target:
        return [], "请提供 target（文件/目录绝对路径或网址）"

    if action == "javascript":
        return ["analyze-javascript-application", target, "--format", "json"], ""
    if action == "audit":
        return ["audit-release-exposure", target, "--format", "json"], ""
    if action == "overview":
        return ["analyze", target, "--format", "json"], ""

    # auto：网址/前端目录走 JS 与网页静态分析，其它交给 analyze 总览
    if target.lower().startswith(("http://", "https://")):
        return ["analyze-web-bundle", target, "--format", "json"], ""
    p = Path(target)
    if p.is_dir():
        return ["analyze-javascript-application", target, "--format", "json"], ""
    if p.suffix.lower() in (".asar", ".js", ".mjs", ".cjs", ".ts"):
        return ["analyze-javascript-application", target, "--format", "json"], ""
    return ["analyze", target, "--format", "json"], ""


def run(target="", action="auto", args="", timeout=300):
    node, script = _node_exe(), _rea_script()
    if not node or not script:
        return ("❌ REA 运行时缺失。预期位置：\n"
                f"  {_RUNTIME}\\node-v24*\\node.exe\n"
                f"  {_RUNTIME}\\rea\\node_modules\\rea-agents\\scripts\\rea.mjs\n"
                "参考：npm install rea-agents --prefix <该目录>/rea（需 Node 24）")

    argv, err = _pick_argv(action, target, args)
    if err:
        return err
    try:
        limit = max(30, min(int(timeout or 300), 1800))
    except (TypeError, ValueError):
        limit = 300

    t0 = time.time()
    try:
        p = subprocess.run([str(node), str(script)] + argv,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=limit)
    except subprocess.TimeoutExpired:
        return f"⏱ REA 超过 {limit}s 未完成，已终止：{argv[0]}。可换更小的目标或调大 timeout。"
    except Exception as e:
        return f"REA 启动失败: {e}"

    cost = time.time() - t0
    out = (p.stdout or "").strip()
    err_tail = (p.stderr or "").strip()[-600:]

    data, summary, saved = None, "", ""
    if out:
        try:
            data = json.loads(out)
        except ValueError:
            # 个别命令输出多段 JSON/带前缀，取第一段完整对象再试
            start = out.find("{")
            try:
                data = json.loads(out[start:]) if start >= 0 else None
            except ValueError:
                data = None
    if data is not None:
        summary = _summarize(data)
        try:
            _OUT_DIR.mkdir(parents=True, exist_ok=True)
            saved = _OUT_DIR / f"rea_{time.strftime('%Y%m%d_%H%M%S')}.json"
            saved.write_text(out, encoding="utf-8")
        except Exception:
            saved = ""
    elif out:
        summary = _clip(out, 1800)

    lines = [f"REA {argv[0]} 退出码 {p.returncode}，耗时 {cost:.1f}s"]
    if summary:
        lines.append(summary)
    if saved:
        try:
            size_mb = saved.stat().st_size / 1024 / 1024
            lines.append(f"完整 JSON: {saved}（{size_mb:.1f} MB，可用 code_search / file_read 取细节）")
        except Exception:
            lines.append(f"完整 JSON: {saved}")
    if p.returncode != 0 and err_tail:
        lines.append("stderr 尾部:\n" + err_tail)
    return "\n".join(lines)


if __name__ == "__main__":
    print(run(action="doctor"))

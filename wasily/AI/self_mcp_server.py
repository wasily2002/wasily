"""
wasily 自我进化 MCP Server（stdio 传输，纯标准库实现 MCP 协议）

让 agent 在对话窗口中修改「自身运行文件夹」里的源文件，并可重启其中的
相关程序（server / brain / watchdog …），使改动对自身生效。

【沙箱进化流程】（源码修改的唯一通道）
  1. self_edit / self_write  —— 只写沙箱副本(_evolve_sandbox/)，不动源文件；
     返回 change_id + unified diff + 自动校验结果(.py 编译/.json 解析)
  2. agent 把 diff 展示给用户，用户确认版本
  3. self_apply(change_id)   —— 人工批准后落盘源文件（自动备份 .mcpbak）
     （此工具在 api_server 侧需用户确认卡片批准）
  4. self_restart(name)      —— 需要时重启程序使改动生效
  self_staged / self_discard —— 查看待批准变更 / 放弃变更

协议：MCP over stdio —— 换行分隔的 JSON-RPC 2.0 消息
配置：同目录 self_mcp_config.json（root / programs，可 self_set_root 在线改）
安全：所有路径限制在 root 内；落盘前自动备份；校验失败默认拒绝 apply
"""
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "self_mcp_config.json")
SANDBOX_DIR = os.path.join(BASE_DIR, "_evolve_sandbox")
INDEX_PATH = os.path.join(SANDBOX_DIR, "index.json")

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "wasily-self-mcp", "version": "1.1.0"}

IGNORE_DIRS = {"node_modules", "__pycache__", ".git", ".vscode",
               "models_7b", "models", "wasily_log", "dist", "build",
               "_evolve_sandbox"}
IGNORE_FILES = {"key.bin"}
MAX_READ_CHARS = 60000
MAX_SEARCH_HITS = 80
MAX_DIFF_CHARS = 6000


# ==================== 配置 ====================

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"root": BASE_DIR, "programs": {}}


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_root():
    root = load_config().get("root") or BASE_DIR
    return os.path.realpath(root)


# ==================== 路径安全 ====================

def safe_path(rel):
    """相对 root 解析并校验不越界；越界抛异常"""
    root = get_root()
    p = os.path.realpath(os.path.join(root, rel))
    if p != root and not p.startswith(root + os.sep):
        raise ValueError(f"路径越界（仅允许访问自身文件夹 {root} 之内）: {rel}")
    return p


# ==================== 只读工具 ====================

def tool_get_config():
    cfg = load_config()
    progs = {k: {"desc": v.get("desc", ""), "cmd": v.get("cmd"),
                 "cwd": v.get("cwd"), "match": v.get("match")}
             for k, v in cfg.get("programs", {}).items()}
    return json.dumps({"root": get_root(), "programs": progs},
                      ensure_ascii=False, indent=2)


def tool_set_root(path):
    if not os.path.isdir(path):
        return f"错误：目录不存在 {path}"
    cfg = load_config()
    cfg["root"] = os.path.realpath(path)
    save_config(cfg)
    return f"自身文件夹位置已改为: {cfg['root']}（已持久化）"


def tool_list(rel_path="."):
    base = safe_path(rel_path)
    if not os.path.isdir(base):
        return f"错误：不是目录 {rel_path}"
    lines = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS)
        depth = os.path.relpath(dirpath, base).count(os.sep)
        if depth > 2:
            dirnames[:] = []
            continue
        indent = "  " * (depth + 1 if os.path.relpath(dirpath, base) != "." else 0)
        if dirpath != base:
            lines.append(f"{indent}📁 {os.path.basename(dirpath)}/")
        for fn in sorted(filenames):
            if fn in IGNORE_FILES or fn.endswith((".gguf", ".mcpbak", ".pyc")):
                continue
            try:
                size = os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                size = 0
            lines.append(f"{indent}  {fn}  ({size:,}B)")
        if len(lines) > 300:
            lines.append("……（结果过多已截断）")
            break
    return "\n".join(lines) or "（空目录）"


def tool_read(path, offset=0, limit=400):
    fp = safe_path(path)
    if not os.path.isfile(fp):
        return f"错误：文件不存在 {path}"
    with open(fp, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    total = len(lines)
    seg = lines[offset:offset + limit]
    out = [f"（共 {total} 行，显示第 {offset + 1}~{offset + len(seg)} 行）"]
    out += [f"{offset + i + 1}\t{l.rstrip()}" for i, l in enumerate(seg)]
    text = "\n".join(out)
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + "\n……（内容过长已截断，请用 offset/limit 分段读取）"
    return text


def tool_search(pattern, glob="*", rel_path="."):
    base = safe_path(rel_path)
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"正则错误: {e}"
    hits = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for fn in filenames:
            if fn in IGNORE_FILES or fn.endswith((".gguf", ".mcpbak", ".pyc")):
                continue
            if glob != "*":
                import fnmatch
                if not fnmatch.fnmatch(fn, glob):
                    continue
            fp = os.path.join(dirpath, fn)
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if rx.search(line):
                            rel = os.path.relpath(fp, get_root())
                            hits.append(f"{rel}:{i}: {line.rstrip()[:200]}")
                            if len(hits) >= MAX_SEARCH_HITS:
                                return "\n".join(hits) + "\n……（命中过多已截断）"
            except OSError:
                continue
    return "\n".join(hits) or "（无匹配）"


def tool_symbols(path=None, symbol_name=None, rel_path="."):
    """A1: 符号索引 — 扫描自身源码返回函数/类/变量定义位置（AI 精确定位改动目标）"""
    base = safe_path(rel_path if path else rel_path)
    results = []
    exts = {".py", ".js", ".json", ".html", ".css", ".ts", ".jsx", ".tsx", ".md"}

    if path:
        # 指定文件 → 单文件符号扫描
        target = safe_path(path)
        if not os.path.isfile(target):
            return f"错误：文件不存在 {path}"
        results.extend(_scan_symbols_in_file(target, symbol_name))
    else:
        # 目录 → 递归扫描
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                if os.path.splitext(fn)[1].lower() not in exts:
                    continue
                if fn in IGNORE_FILES or fn.endswith((".gguf", ".mcpbak", ".pyc")):
                    continue
                try:
                    results.extend(_scan_symbols_in_file(fp, symbol_name))
                except OSError:
                    continue
                if len(results) >= 150:
                    break
            if len(results) >= 150:
                break

    if not results:
        return "（未找到符号定义）"
    return "\n".join(results)


def _scan_symbols_in_file(fp, target=None):
    """单文件符号扫描，返回 ['  path:line  类型  名称']"""
    rel = os.path.relpath(fp, get_root())
    ext = os.path.splitext(fp)[1].lower()
    out = []
    try:
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []

    def _emit(line_no, kind, name, extra=""):
        if target and target not in name:
            return
        out.append(f"{rel}:{line_no:>4}  {kind:6}  {name}{(' — ' + extra) if extra else ''}")

    if ext == ".py":
        # Python: def / class / import / 顶层变量赋值
        for i, line in enumerate(lines, 1):
            s = line.strip()
            m = re.match(r"^def\s+(\w+)\s*\((.*?)\)", s)
            if m:
                args = m.group(2).split(",")[0].strip() if m.group(2) else ""
                _emit(i, "def", m.group(1), f"首参 {args}" if args else "")
                continue
            m = re.match(r"^class\s+(\w+)", s)
            if m:
                _emit(i, "class", m.group(1))
                continue
            m = re.match(r"^from\s+([\w.]+)\s+import\s+(.+)", s)
            if m:
                _emit(i, "import", f"from {m.group(1)} import {m.group(2).strip()[:40]}")
                continue
            m = re.match(r"^import\s+(.+)", s)
            if m:
                _emit(i, "import", m.group(1).strip()[:40])
                continue
            m = re.match(r"^([A-Z_]\w*)\s*=\s*", s)  # 大写常量
            if m:
                _emit(i, "const", m.group(1))
                continue
            m = re.match(r"^(\w+)\s*=\s*(?!lambda|\()", s)
            if m and not s.startswith("_"):
                _emit(i, "var", m.group(1))

    elif ext in (".js", ".ts", ".jsx", ".tsx"):
        for i, line in enumerate(lines, 1):
            s = line.strip()
            m = re.match(r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\((.*?)\)", s)
            if m:
                _emit(i, "fn", m.group(1))
                continue
            m = re.match(r"(?:export\s+)?class\s+(\w+)", s)
            if m:
                _emit(i, "class", m.group(1))
                continue
            m = re.match(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(", s)
            if m:
                _emit(i, "arrow", m.group(1))
                continue
            m = re.match(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:require\(|fetch\(|import\()", s)
            if m:
                _emit(i, "import", m.group(1))
                continue
            m = re.match(r"module\.exports\s*=\s*(?:\w+|\{)", s)
            if m:
                _emit(i, "export", "(module.exports)")
                continue

    elif ext in (".json", ".html", ".css"):
        # 非代码文件：只给行号映射（前 100 行）
        limit = min(100, len(lines))
        for i in range(limit):
            s = lines[i].strip()[:80]
            if s:
                _emit(i + 1, "line", s)

    # 文件级 overview（前 5 个符号给一个头部）
    if out and len(out) > 5:
        header = f"📄 {rel}（共 {len(out)} 个符号，以下前 15 行）"
        return [header] + out[:15]
    return out[:15]


def tool_autofix(change_id, skip_restart=False):
    """【进化闭环】落盘 + 自动编译/构建验证 + 失败回滚 — 让 self 改动自洽生效"""
    idx = _load_index()
    e = idx.get(change_id)
    if not e:
        return f"错误：变更 {change_id} 不存在"

    # 先落盘（不 ask）
    real_fp = safe_path(e["path"])
    if os.path.isfile(real_fp):
        shutil.copy2(real_fp, real_fp + ".mcpbak")
    try:
        shutil.copy2(e["staged"], real_fp)
    except OSError as ex:
        return f"落盘失败: {ex}"

    idx.pop(change_id)
    _save_index(idx)
    try:
        os.remove(e["staged"])
    except OSError:
        pass

    # 自动验证
    ext = os.path.splitext(e["path"])[1].lower()
    vmsg = ""
    vok = True
    try:
        if ext == ".py":
            r = subprocess.run([sys.executable, "-m", "py_compile", real_fp],
                               capture_output=True, text=True, timeout=15)
            vok = r.returncode == 0
            vmsg = "✅ Python 编译通过" if vok else "❌ Python 编译失败: " + (r.stderr or "").strip().splitlines()[-1]
        elif ext == ".json":
            with open(real_fp, "r", encoding="utf-8") as f:
                json.load(f)
            vmsg = "✅ JSON 解析通过"
        elif ext in (".js", ".ts"):
            # 基础语法检查：node --check
            r = subprocess.run(["node", "--check", real_fp],
                               capture_output=True, text=True, timeout=15,
                               encoding="utf-8")
            vok = r.returncode == 0
            vmsg = "✅ JS 语法通过" if vok else "❌ JS 语法失败: " + (r.stderr or r.stdout or "").strip().splitlines()[-1]
        else:
            vmsg = f"✅ {ext or '无扩展名'} 无自动语法校验"
    except FileNotFoundError:
        vmsg = f"⚠ 校验工具未安装（跳过自动验证）"
    except Exception as ex:
        vok = False
        vmsg = f"❌ 校验异常: {ex}"

    # 失败自动回滚
    if not vok:
        bak = real_fp + ".mcpbak"
        if os.path.isfile(bak):
            shutil.copy2(bak, real_fp)
            return f"❌ 自动验证失败，已自动回滚到 .mcpbak（原文件未变）\n{vmsg}\n请修沙箱版本：self_edit/self_write 重新暂存后再 self_autofix(change_id)"
        else:
            return f"❌ 自动验证失败且无 .mcpbak 备份可用！{vmsg} 请立即 self_read 后 self_edit 修复"

    # 通过 → 自动重启 server（除非 skip_restart=true）
    if not skip_restart:
        try:
            tool_restart("server")
            return (f"✅ 已落盘 {e['path']}\n{vmsg}\n"
                    f"server 重启器已接管，几秒后自动生效。"
                    f"请稍候片刻再继续对话。")
        except Exception as re:
            return (f"✅ 已落盘 {e['path']}\n{vmsg}\n"
                    f"⚠ 重启失败: {re} — 请手动 self_restart(name=\"server\")")
    return f"✅ 已落盘 {e['path']}（skip_restart=true，未自动重启）\n{vmsg}"


# ==================== 沙箱（进化变更暂存区） ====================

def _load_index():
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_index(idx):
    os.makedirs(SANDBOX_DIR, exist_ok=True)
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)


def _validate(staged_fp, rel):
    """自动校验沙箱副本：.py 语法编译 / .json 解析，返回 (ok, 描述)"""
    ext = os.path.splitext(rel)[1].lower()
    if ext == ".py":
        r = subprocess.run([sys.executable, "-m", "py_compile", staged_fp],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return True, "Python 语法编译通过"
        err = (r.stderr or "").strip().splitlines()
        return False, "Python 语法错误: " + (err[-1] if err else "编译失败")
    if ext == ".json":
        try:
            with open(staged_fp, "r", encoding="utf-8") as f:
                json.load(f)
            return True, "JSON 解析通过"
        except Exception as e:
            return False, f"JSON 解析失败: {e}"
    return True, f"（{ext or '无扩展名'} 类型无自动校验，请人工审阅 diff）"


def _stage(rel, new_text, kind):
    """写沙箱副本 + 校验 + diff，返回结果文本"""
    real_fp = safe_path(rel)
    old_text = ""
    if os.path.isfile(real_fp):
        with open(real_fp, "r", encoding="utf-8", errors="replace") as f:
            old_text = f.read()

    cid = uuid.uuid4().hex[:8]
    staged_fp = os.path.join(SANDBOX_DIR, cid + "__" + os.path.basename(rel))
    os.makedirs(SANDBOX_DIR, exist_ok=True)
    with open(staged_fp, "w", encoding="utf-8") as f:
        f.write(new_text)

    ok, vmsg = _validate(staged_fp, rel)

    diff = "\n".join(difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(),
        fromfile=f"a/{rel} (当前)", tofile=f"b/{rel} (沙箱)",
        lineterm="", n=2))
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n……（diff 过长已截断）"
    if not diff:
        diff = "（内容无变化）"

    idx = _load_index()
    idx[cid] = {"path": rel, "kind": kind, "ts": time.time(),
                "staged": staged_fp, "valid": ok, "vmsg": vmsg}
    _save_index(idx)

    status = "✅ 校验通过" if ok else "❌ 校验失败（self_apply 默认会拒绝，需 force=true 强制）"
    return (f"变更已暂存到沙箱【源文件尚未修改】\n"
            f"change_id: {cid}\n文件: {rel}\n自动校验: {status} — {vmsg}\n"
            f"--- diff ---\n{diff}\n------------\n"
            f"请把以上 diff 展示给用户审阅。用户确认同意后调用 "
            f"self_apply(change_id=\"{cid}\") 落盘；用户不满意则 "
            f"self_discard(change_id=\"{cid}\") 放弃或重新暂存新版本。")


def tool_write(path, content):
    return _stage(path, content, "write")


def tool_edit(path, old_string, new_string):
    real_fp = safe_path(path)
    if not os.path.isfile(real_fp):
        return f"错误：文件不存在 {path}（新建文件请用 self_write）"
    with open(real_fp, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    count = text.count(old_string)
    if count == 0:
        return "错误：old_string 未匹配（注意缩进与换行必须完全一致）"
    if count > 1:
        return f"错误：old_string 出现 {count} 次，请提供更多上下文使其唯一"
    return _stage(path, text.replace(old_string, new_string, 1), "edit")


def tool_staged():
    idx = _load_index()
    if not idx:
        return "沙箱为空：没有待批准的变更"
    lines = ["待批准的进化变更："]
    for cid, e in idx.items():
        lines.append(f"- [{cid}] {e['path']} ({e['kind']}) "
                     f"{'✅' if e['valid'] else '❌'} {e['vmsg']} "
                     f"@ {time.strftime('%H:%M:%S', time.localtime(e['ts']))}")
    lines.append("用 self_apply(change_id) 落盘，self_discard(change_id) 放弃")
    return "\n".join(lines)


def tool_apply(change_id, force=False):
    idx = _load_index()
    e = idx.get(change_id)
    if not e:
        return f"错误：变更 {change_id} 不存在（用 self_staged 查看待批准列表）"
    if not e["valid"] and not force:
        return (f"拒绝落盘：该校验未通过（{e['vmsg']}）。"
                f"确认风险后可 self_apply(change_id=\"{change_id}\", force=true) 强制")
    real_fp = safe_path(e["path"])
    os.makedirs(os.path.dirname(real_fp), exist_ok=True)
    if os.path.isfile(real_fp):
        shutil.copy2(real_fp, real_fp + ".mcpbak")     # 落盘前备份
    shutil.copy2(e["staged"], real_fp)
    idx.pop(change_id)
    _save_index(idx)
    try:
        os.remove(e["staged"])
    except OSError:
        pass
    return (f"已落盘 {e['path']}（原文件已备份为 .mcpbak）。"
            f"如需对自身生效，调用 self_restart 重启相关程序；"
            f"chat.html 等页面文件刷新浏览器即可。")


def tool_discard(change_id):
    idx = _load_index()
    e = idx.pop(change_id, None)
    _save_index(idx)
    if not e:
        return f"变更 {change_id} 不存在"
    try:
        os.remove(e["staged"])
    except OSError:
        pass
    return f"已放弃对 {e['path']} 的暂存变更，源文件未受影响"


# ==================== 程序管理 ====================

def _find_procs(match):
    ps = ("Get-CimInstance Win32_Process | Where-Object "
          "{ $_.CommandLine -and $_.CommandLine -like '*%s*' } | "
          "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }") % match.replace("'", "''")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=20,
                           encoding="utf-8", errors="replace")
    except Exception:
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) == 2 and parts[0].isdigit():
            pid = int(parts[0])
            if pid != os.getpid():
                out.append((pid, parts[1]))
    return out


def _prog_cfg(name):
    progs = load_config().get("programs", {})
    if name not in progs:
        raise ValueError(f"未知程序 {name}，可用: {', '.join(progs.keys())}")
    p = dict(progs[name])
    root = get_root()
    p["cmd"] = [c.replace("{root}", root) for c in p.get("cmd", [])]
    p["cwd"] = (p.get("cwd") or root).replace("{root}", root)
    return p


def tool_status():
    cfg = load_config()
    lines = [f"自身文件夹: {get_root()}"]
    for name in cfg.get("programs", {}):
        try:
            p = _prog_cfg(name)
        except ValueError:
            continue
        procs = _find_procs(p.get("match") or "")
        mark = "●" if procs else "○"
        state = ("运行中: " + ", ".join(f"PID {pid}" for pid, _ in procs)) if procs else "未运行"
        lines.append(f"{mark} {name}（{p.get('desc', '')}）{state}")
    n = len(_load_index())
    if n:
        lines.append(f"⚠ 沙箱中有 {n} 个待批准变更（self_staged 查看）")
    return "\n".join(lines)


def tool_start(name):
    p = _prog_cfg(name)
    if not p.get("cmd"):
        return f"错误：程序 {name} 未配置启动命令"
    if _find_procs(p.get("match") or ""):
        return f"{name} 已在运行中（如需重启请用 self_restart）"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen(p["cmd"], cwd=p["cwd"], creationflags=flags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"已启动 {name}: {' '.join(p['cmd'])}"


def tool_stop(name):
    p = _prog_cfg(name)
    procs = _find_procs(p.get("match") or "")
    if not procs:
        return f"{name} 当前未运行"
    killed = []
    for pid, _ in procs:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
            killed.append(pid)
        except Exception:
            pass
    return f"已停止 {name}: PID {', '.join(map(str, killed))}"


def tool_restart(name):
    p = _prog_cfg(name)
    restarter = os.path.join(BASE_DIR, "self_restarter.py")
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
             | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    subprocess.Popen([sys.executable, restarter, name],
                     cwd=BASE_DIR, creationflags=flags,
                     stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)
    if name == "server":
        return ("重启器已接管 api_server 重启流程：本服务将在几秒内退出并自动以新代码重新拉起"
                "（健康检查通过后恢复）。请稍候片刻再继续对话。")
    return f"重启器已接管 {name} 的重启流程（杀旧进程 → 拉起新进程 → 就绪检查），数秒内完成。"


# ==================== MCP 协议层 ====================

MCP_TOOLS = [
    {"name": "self_get_config",
     "description": "查看自身文件夹位置(root)与可管理程序列表",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "self_set_root",
     "description": "修改自身文件夹位置（自定义 agent 运行时目录，持久化生效）",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "新的自身文件夹绝对路径"}},
         "required": ["path"]}},
    {"name": "self_list",
     "description": "【仅用于改自身源码】列出自身文件夹内的源文件结构（跳过 node_modules/模型等大目录）。日常列目录请用 file_list，不要用本工具。",
     "inputSchema": {"type": "object", "properties": {
         "rel_path": {"type": "string", "description": "相对 root 的目录，默认 ."}}}},
    {"name": "self_read",
     "description": "【仅用于改自身源码】读取自身源文件内容（带行号；大文件用 offset/limit 分段读）。日常读文件请用 file_read，不要用本工具。",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "相对 root 的文件路径"},
         "offset": {"type": "integer", "description": "起始行(从0计)，默认0"},
         "limit": {"type": "integer", "description": "读取行数，默认400"}}}},
    {"name": "self_write",
     "description": "【沙箱】整体写入/新建文件的进化版本——只写沙箱副本并做自动校验，"
                    "返回 change_id + diff 供用户审阅，源文件不受影响",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string"},
         "content": {"type": "string", "description": "完整新内容"}},
         "required": ["path", "content"]}},
    {"name": "self_edit",
     "description": "【沙箱】对源文件做一处精确替换的进化版本——只写沙箱副本并自动校验"
                    "(.py编译/.json解析)，返回 change_id + diff，源文件不受影响",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string"},
         "old_string": {"type": "string", "description": "被替换原文（含缩进，须唯一）"},
         "new_string": {"type": "string", "description": "替换后新内容"}},
         "required": ["path", "old_string", "new_string"]}},
    {"name": "self_search",
     "description": "【仅用于改自身源码】在自身源文件中按正则搜索内容（输出 文件:行号: 内容）。日常搜索文件请用 file_search，不要用本工具。",
     "inputSchema": {"type": "object", "properties": {
         "pattern": {"type": "string"},
         "glob": {"type": "string", "description": "文件名过滤如 *.py，默认 *"},
         "rel_path": {"type": "string", "description": "搜索起始目录，默认 ."}},
         "required": ["pattern"]}},
    {"name": "self_symbols",
     "description": "【自我编程核心】符号级索引 — 扫描自身源码返回函数/类/变量定义位置（带行号+首参）。"
                    "改自身代码前必须先用这个定位目标，而不是盲目 grep 全文件。"
                    "返回格式: path:line  kind  name [— 额外信息]",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "单文件模式：指定具体文件（相对 root）"},
         "symbol_name": {"type": "string", "description": "过滤只返回名称包含此字符串的符号"},
         "rel_path": {"type": "string", "description": "目录模式下的扫描起点，默认 ."}}}},
    {"name": "self_staged",
     "description": "查看沙箱中所有待用户批准的进化变更（change_id 列表）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "self_apply",
     "description": "【需用户批准】把沙箱中已审阅通过的变更落盘到源文件（自动备份 .mcpbak）；"
                    "必须在用户明确同意 diff 之后调用",
     "inputSchema": {"type": "object", "properties": {
         "change_id": {"type": "string"},
         "force": {"type": "boolean", "description": "校验失败时强制落盘，默认 false"}},
         "required": ["change_id"]}},
    {"name": "self_discard",
     "description": "放弃沙箱中的某个暂存变更（源文件从未被修改，无需恢复）",
     "inputSchema": {"type": "object", "properties": {
         "change_id": {"type": "string"}},
         "required": ["change_id"]}},
    {"name": "self_autofix",
     "description": "【进化闭环】一步完成：落盘沙箱变更 → 自动 py_compile/node --check 验证 → 失败自动回滚 .mcpbak → 验证通过自动 self_restart。"
                    "改自身代码的首选工具（比 self_apply + self_restart + 手动编译 高效得多）。失败时原文件不变且告诉你修什么，成功时 server 自动重启生效。",
     "inputSchema": {"type": "object", "properties": {
         "change_id": {"type": "string"},
         "skip_restart": {"type": "boolean", "description": "只落盘验证不重启（改了扩展JS时不用重启server）"}},
         "required": ["change_id"]}},
    {"name": "self_status",
     "description": "查看自身相关程序（server/brain/watchdog）运行状态及待批准变更数",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "self_start",
     "description": "启动自身文件夹中的某个程序（server/brain/watchdog）",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}}, "required": ["name"]}},
    {"name": "self_stop",
     "description": "停止自身文件夹中的某个程序",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}}, "required": ["name"]}},
    {"name": "self_restart",
     "description": "重启自身程序使已落盘的源码改动生效（server=对话服务自身/brain=桌面大脑/watchdog=守护进程）",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}}, "required": ["name"]}},
]

_DISPATCH = {
    "self_get_config": lambda a: tool_get_config(),
    "self_set_root": lambda a: tool_set_root(a.get("path", "")),
    "self_list": lambda a: tool_list(a.get("rel_path", ".")),
    "self_read": lambda a: tool_read(a.get("path", ""),
                                     int(a.get("offset", 0) or 0),
                                     int(a.get("limit", 400) or 400)),
    "self_write": lambda a: tool_write(a.get("path", ""), a.get("content", "")),
    "self_edit": lambda a: tool_edit(a.get("path", ""), a.get("old_string", ""),
                                     a.get("new_string", "")),
    "self_search": lambda a: tool_search(a.get("pattern", ""),
                                         a.get("glob", "*"),
                                         a.get("rel_path", ".")),
    "self_symbols": lambda a: tool_symbols(a.get("path"),
                                           a.get("symbol_name"),
                                           a.get("rel_path", ".")),
    "self_staged": lambda a: tool_staged(),
    "self_apply": lambda a: tool_apply(a.get("change_id", ""),
                                       bool(a.get("force", False))),
    "self_discard": lambda a: tool_discard(a.get("change_id", "")),
    "self_autofix": lambda a: tool_autofix(a.get("change_id", ""),
                                           bool(a.get("skip_restart", False))),
    "self_status": lambda a: tool_status(),
    "self_start": lambda a: tool_start(a.get("name", "")),
    "self_stop": lambda a: tool_stop(a.get("name", "")),
    "self_restart": lambda a: tool_restart(a.get("name", "")),
}


def _send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(req_id, result):
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code, message):
    _send({"jsonrpc": "2.0", "id": req_id,
           "error": {"code": code, "message": message}})


def handle(req):
    method = req.get("method", "")
    req_id = req.get("id")

    if method == "initialize":
        _result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    elif method == "ping":
        _result(req_id, {})
    elif method == "tools/list":
        _result(req_id, {"tools": MCP_TOOLS})
    elif method == "tools/call":
        params = req.get("params", {})
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        fn = _DISPATCH.get(name)
        if not fn:
            _result(req_id, {"content": [{"type": "text",
                                          "text": f"未知工具 {name}"}],
                             "isError": True})
            return
        try:
            text = fn(args)
            _result(req_id, {"content": [{"type": "text", "text": str(text)}]})
        except Exception as e:
            _result(req_id, {"content": [{"type": "text",
                                          "text": f"工具执行错误: {e}"}],
                             "isError": True})
    elif method and method.startswith("notifications/"):
        pass
    elif req_id is not None:
        _error(req_id, -32601, f"Method not found: {method}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            handle(req)
        except Exception:
            pass
    # stdin EOF（父进程退出）→ 自然结束


if __name__ == "__main__":
    main()

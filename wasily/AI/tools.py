"""
AI 工具集 - 完整系统控制权限
包括：文件操作、代码执行、应用控制、鼠标键盘控制、
      进程管理、剪贴板、截图、窗口管理、网络操作、
      电源管理、系统信息、注册表、环境变量
"""
import os
import re
import json
import inspect
import subprocess
import sys
import tempfile
import time
import shutil
import ctypes
import platform
from pathlib import Path

# Windows 下隐藏子进程控制台窗口：终端命令输出回到 agent 内嵌终端，不再弹任务栏黑框
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

_AI_DIR = os.path.dirname(os.path.abspath(__file__))
if _AI_DIR not in sys.path:
    sys.path.insert(0, _AI_DIR)

import config as _cfg  # 配置中心（devices.enabled 等）
import readers  # 统一读取层（工具契约 v2）

# 尝试导入可选库
try:
    import pyautogui
    pyautogui.FAILSAFE = False  # 禁用安全角检测
    HAS_PYAUTOGUI = True
except Exception:
    HAS_PYAUTOGUI = False

try:
    from PIL import ImageGrab
    HAS_PIL = True
except Exception:
    HAS_PIL = False

try:
    import psutil
    HAS_PSUTIL = True
except Exception:
    HAS_PSUTIL = False

try:
    import pyperclip
    HAS_CLIPBOARD = True
except Exception:
    HAS_CLIPBOARD = False


# ============ 文件操作 ============

_TOOLS_BASE = os.path.dirname(os.path.abspath(__file__))

# ============ 当前项目作用域（默认根 = IDE 上报的当前工作区） ============
# 按线程隔离：同一进程可能同时处理多个窗口/会话，不能互相串根。
import threading as _threading
_WS_TLS = _threading.local()
_WS_LAST = {"root": ""}


def set_workspace_root(root, isolated=False):
    """记录本次请求的项目根目录；isolated=True 时把越界访问从警告升级为拒绝。"""
    r = str(root or "").strip()
    _WS_TLS.root = r
    _WS_TLS.isolated = bool(isolated and r)
    if r:
        _WS_LAST["root"] = r
    return r


def get_workspace_root():
    """当前项目根：本线程设置过就严格用它（哪怕是空串，避免沿用上一个项目）；
    只有还没设置过的后台线程才回退到最近一次有效设置。"""
    if hasattr(_WS_TLS, "root"):
        return str(getattr(_WS_TLS, "root") or "")
    return str(_WS_LAST.get("root") or "")


def get_workspace_isolated():
    """只有显式传播了隔离标记的线程才启用严格边界，不能用全局值兜底。"""
    return bool(getattr(_WS_TLS, "isolated", False))


class WorkspaceBoundaryError(PermissionError):
    pass


def _path_in_workspace(path, root):
    try:
        candidate = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        base = os.path.normcase(os.path.realpath(os.path.abspath(root)))
        return os.path.commonpath([candidate, base]) == base
    except (OSError, ValueError):
        return False


def scope_path(path):
    """把路径解析到当前项目根；隔离运行中，相对路径穿越和外部绝对路径一律拒绝。"""
    raw = str(path or "").strip()
    root = get_workspace_root()
    if not root or not raw:
        return raw, ""
    p = Path(raw)
    resolved = str(Path(root) / raw) if not p.is_absolute() else raw
    if _path_in_workspace(resolved, root):
        return resolved, ""
    if get_workspace_isolated():
        raise WorkspaceBoundaryError("隔离运行禁止访问 worktree 外路径: %s" % raw)
    return raw, f"[超出当前项目] {raw} 不在当前项目根 {root} 内；仅当用户明确要求时才可访问。\n"


def _isolated_command_error(command):
    """静态拦截明显的目录穿越与外部路径；cwd 本身不作为安全边界。"""
    text = str(command or "")
    if re.search(r"\.\.[\\/]", text) or re.search(r"(?:^|[\s\"'=])\.\.(?:$|\s)", text):
        return "隔离运行禁止命令中的 .. 目录穿越"
    if re.search(r"(?i)\b(powershell|pwsh|cmd)(?:\.exe)?\s+/(?:c|k)\b", text):
        return "隔离运行禁止嵌套 shell，请直接执行目标命令"
    pattern = r"(?:[A-Za-z]:[\\/]|\\\\)[^\s&|;<>\"']+"
    if os.name != "nt":
        pattern = r"(?<![\w:])/(?:[^\s&|;<>\"']+)"
    for match in re.finditer(pattern, text):
        candidate = match.group(0).rstrip(",)")
        if not _path_in_workspace(candidate, get_workspace_root()):
            return "隔离运行禁止命令访问 worktree 外路径: %s" % candidate
    return ""


def _with_scope_note(result, note):
    """把「超出当前项目」标注塞进结果（JSON 结果塞进 meta，纯文本结果前置一行）"""
    if not note:
        return result
    try:
        d = json.loads(result)
        if isinstance(d, dict) and d.get("ok"):
            meta = d.get("meta")
            if isinstance(meta, dict):
                meta["scope_warning"] = note.strip()
                return json.dumps(d, ensure_ascii=False)
    except Exception:
        pass
    return note + str(result)


def _resolve_file_path(path):
    """路径不存在时按文件名自动定位，返回 (Path, 提示文本)。

    模型偶尔会猜错绝对路径（如把 wasily/AI/photo3d.js 写成 D:\\煮饭仔点餐\\AI\\photo3d.js）。
    这里先在自身源码目录和当前工作目录找同名文件，再在源码目录内浅层递归；
    唯一命中时返回纠正后的路径，多个命中则返回候选列表让模型明确指定。
    """
    p = Path(path)
    if p.exists():
        return p, ""
    if get_workspace_isolated():
        return p, "文件不存在: %s（隔离运行已禁用跨根自动定位）" % path
    name = p.name
    if not name:
        return p, f"文件不存在: {path}"
    hits, seen = [], set()
    _roots = [r for r in (get_workspace_root(), _TOOLS_BASE, os.getcwd()) if r]
    for root in _roots:
        fp = os.path.join(root, name)
        if os.path.isfile(fp) and fp.lower() not in seen:
            seen.add(fp.lower())
            hits.append(fp)
    if not hits:
        for dirpath, dirnames, filenames in os.walk(_TOOLS_BASE):
            if dirpath[len(_TOOLS_BASE):].count(os.sep) >= 2:
                dirnames[:] = []
                continue
            if name in filenames:
                fp = os.path.join(dirpath, name)
                if fp.lower() not in seen:
                    seen.add(fp.lower())
                    hits.append(fp)
    if len(hits) == 1:
        return Path(hits[0]), f"[路径纠正] 未找到 {path}，已按文件名定位到 {hits[0]}\n"
    if len(hits) > 1:
        return p, ("文件不存在: %s\n找到多个同名文件，请用完整路径重试：\n%s"
                   % (path, "\n".join(hits[:5])))
    near = []
    try:
        stem = p.stem.lower()
        for dirpath, dirnames, filenames in os.walk(_TOOLS_BASE):
            if dirpath[len(_TOOLS_BASE):].count(os.sep) >= 2:
                dirnames[:] = []
                continue
            for fn in filenames:
                if stem and stem in fn.lower():
                    near.append(os.path.join(dirpath, fn))
    except Exception:
        pass
    msg = f"文件不存在: {path}"
    if near:
        msg += "\n相近文件（请用完整路径重试）：\n" + "\n".join(near[:5])
    return p, msg


def _current_session_id():
    """当前请求的会话 id：命令事件带上它，页面能判断归属。

    只能从 sys.modules 里取现成模块：服务以 `python api_server.py` 启动时
    模块名是 __main__，再写 `import api_server` 会把整个服务重新执行一遍
    （单实例检测 → sys.exit），在请求线程里表现就是连接被直接断开。
    """
    try:
        for key in ("api_server", "__main__"):
            mod = sys.modules.get(key)
            shim = getattr(mod, "_CUR_SESSION", None) if mod is not None else None
            if shim is not None:
                return str(shim["sid"])
    except Exception:
        pass
    return ""


def shell_exec(command: str, timeout: int = 60, cwd: str = "") -> str:
    """在指定项目目录执行 shell 命令；隔离运行时拒绝越界路径。"""
    try:
        workdir = get_workspace_root() or os.getcwd()
        if cwd:
            workdir, _ = scope_path(cwd)
        if get_workspace_isolated():
            error = _isolated_command_error(command)
            if error:
                return "⚠️ " + error
        # 统一走命令总线：Agent 窗口与 IDE 面板订阅同一条事件流，
        # 因此任一侧执行命令，另一侧都能看到启动、逐行输出与结束/中断状态。
        try:
            import command_bus as _bus
            res = _bus.run_sync(command, cwd=workdir, timeout=timeout,
                                source="agent", session_id=_current_session_id())
            if res.get("status") == "timeout":
                return f"命令超时（{timeout}秒）"
            output = res.get("output") or ""
            if output:
                return output
            if res.get("status") == "failed" and res.get("reason"):
                return f"执行错误: {res['reason']}"
            return "(命令执行完毕，无输出)"
        except Exception:  # noqa: BLE001 — 总线不可用时退回旧路径，工具本身不能失效
            pass
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
            cwd=workdir, creationflags=_NO_WINDOW
        )
        output = result.stdout
        if result.stderr:
            output += "\n[stderr]\n" + result.stderr
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return output.strip() if output.strip() else "(命令执行完毕，无输出)"
    except subprocess.TimeoutExpired:
        return f"命令超时（{timeout}秒）"
    except Exception as e:
        return f"执行错误: {str(e)}"


def shell_admin(command: str, timeout: int = 60) -> str:
    """以管理员权限执行命令"""
    if get_workspace_isolated():
        return ISOLATED_REJECT_MSG + ": shell_admin"
    try:
        # 使用 runas 或 Start-Process -Verb RunAs
        ps_cmd = f'Start-Process cmd -ArgumentList "/c {command}" -Verb RunAs -Wait'
        result = subprocess.run(
            ["powershell", "-Command", ps_cmd],
            capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace"
        )
        output = result.stdout
        if result.stderr:
            output += "\n[stderr]\n" + result.stderr
        return output.strip() if output.strip() else "(管理员命令已执行)"
    except subprocess.TimeoutExpired:
        return f"管理员命令超时（{timeout}秒）"
    except Exception as e:
        return f"管理员执行错误: {str(e)}"


def file_read(path: str, offset: int = 0, limit: int = 0, max_chars: int = 0) -> str:
    """读取文件（契约 v2）：文本/代码按行分段；二进制自动探测并路由到专用解析器。"""
    try:
        _scoped, _scope_note = scope_path(path)
        p, note = _resolve_file_path(_scoped)
        note = _scope_note + note
        if not p.exists():
            return readers.fail("file_read", "FILE_NOT_FOUND",
                                note or f"文件不存在: {path}",
                                "先用 file_search 按文件名定位，或 file_list 查看目录；不要猜绝对路径")
        if p.is_dir():
            return readers.fail("file_read", "PATH_IS_DIR",
                                f"路径是目录，不是文件: {p}",
                                "用 file_list 列出目录，或传具体文件路径")
        cap = int(max_chars) if max_chars else readers.TEXT_MAX
        kind = readers.sniff_kind(p)
        if kind == "text":
            return _with_scope_note(readers.read_text(p, offset=offset, limit=limit, max_chars=cap), _scope_note)
        if kind == "pdf":
            return _with_scope_note(readers.retag(readers.read_pdf(p, max_chars=cap), "file_read",
                                                  {"auto_routed": "read_pdf"}), _scope_note)
        if kind == "office":
            return _with_scope_note(readers.retag(readers.read_office(p, max_chars=cap), "file_read",
                                                  {"auto_routed": "read_office"}), _scope_note)
        if kind == "image":
            return _with_scope_note(readers.retag(readers.read_image(p, max_chars=cap), "file_read",
                                                  {"auto_routed": "read_image"}), _scope_note)
        return _with_scope_note(readers.read_binary_info(p, tool="file_read"), _scope_note)
    except TypeError as e:
        return readers.fail("file_read", "INVALID_ARG", f"参数错误: {e}",
                            "检查 offset/limit 是否为整数")
    except Exception as e:
        return readers.fail("file_read", "INTERNAL_ERROR", f"读取错误: {e}",
                            "重试一次；仍失败用 file_list 确认路径与权限")


def file_write(path: str, content: str) -> str:
    """写入文件"""
    try:
        _scoped, _scope_note = scope_path(path)
        p = Path(_scoped)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return _scope_note + f"文件已写入: {_scoped} ({len(content)} 字符)"
    except Exception as e:
        return f"写入错误: {str(e)}"


def save_output(filename: str, content: str) -> str:
    """把交付给用户的最终产物保存到 agent/outputs（Agent 中心「我的产物」可直接打开）"""
    try:
        out_dir = Path(__file__).resolve().parent / "agent" / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        name = Path(filename).name or "output.txt"
        p = out_dir / name
        p.write_text(content, encoding="utf-8")
        return f"产物已保存: agent/outputs/{name} ({len(content)} 字符)"
    except Exception as e:
        return f"保存产物失败: {str(e)}"


# ============ IDE 桥工具：走编辑器缓冲区，而不是绕过 IDE 直接写磁盘 ============
IDE_BRIDGE = "http://127.0.0.1:18753"


def _ide_bridge(path, payload=None, timeout=15):
    """调用 IDE 扩展的 18753 桥；离线时返回结构化错误而不是抛异常。"""
    import urllib.error
    import urllib.request
    url = IDE_BRIDGE + path
    try:
        if payload is None:
            req = urllib.request.Request(url, method="GET")
        else:
            req = urllib.request.Request(
                url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore") or "{}")
    except urllib.error.HTTPError as e:
        # 桥返回 4xx/5xx 时保留状态码，便于上层选择回退策略
        raw = e.read().decode("utf-8", "ignore")
        try:
            payload = json.loads(raw or "{}")
        except ValueError:
            payload = {"error": raw[:200]}
        payload.setdefault("ok", False)
        payload["status"] = e.code
        return payload
    except urllib.error.URLError as e:
        return {"ok": False, "error": "IDE 桥未连接（127.0.0.1:18753）：%s" % e}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def ide_open_file(path: str, line: int = 0) -> str:
    """在 IDE 编辑器中打开文件并定位到指定行。"""
    full, note = scope_path(path)
    res = _ide_bridge("/open_file", {"path": full, "line": int(line or 0)})
    if not res.get("ok"):
        return "打开失败: %s" % res.get("error")
    return note + str(res.get("detail") or ("已打开 %s" % full))


def ide_read_file(path: str, offset: int = 1, limit: int = 0) -> str:
    """读取编辑器缓冲区（含未保存修改），按行号返回，便于后续精确编辑。"""
    full, note = scope_path(path)
    res = _ide_bridge("/read_file", {"path": full})
    if not res.get("ok"):
        # 旧版 IDE 没有 /read_file 或桥离线时，退回磁盘读取，保证能力不中断
        if res.get("status") in (404, 405) or "未连接" in str(res.get("error")):
            raw = file_read(path, max(0, int(offset or 1) - 1), int(limit or 0))
            return "IDE 桥不可用，已回退磁盘读取：\n" + raw
        return "读取失败: %s" % res.get("error")
    text = str(res.get("text") or "")
    lines = text.splitlines()
    start = max(1, int(offset or 1))
    count = int(limit or 0) or 400
    chunk = lines[start - 1:start - 1 + count]
    body = "\n".join("%5d| %s" % (start + i, ln) for i, ln in enumerate(chunk))
    head = "%s%s（共 %d 行，显示 %d-%d）\n" % (
        note, full, len(lines), start, start + len(chunk) - 1 if chunk else start)
    if not res.get("saved", True):
        head += "（该文件有未保存修改，读取的是编辑器缓冲区）\n"
    return head + body


def ide_edit_file(path: str, old_string: str = "", new_string: str = "",
                  content: str = "") -> str:
    """通过 WorkspaceEdit 修改并保存；old_string 为空表示全文替换。"""
    full, note = scope_path(path)
    new_text = new_string if new_string or not content else content
    if not new_text and not old_string:
        return "编辑失败: 需要 new_string 或 content"
    res = _ide_bridge("/edit_file", {"path": full, "old_string": old_string,
                                     "new_string": new_text}, timeout=30)
    if not res.get("ok"):
        return "编辑失败: %s" % res.get("error")
    return note + str(res.get("detail") or ("已编辑 %s" % full))


def ide_run_terminal(command: str, timeout: int = 120) -> str:
    """在 IDE 的 wasily AI 终端执行命令并回传输出（需 shell integration）。"""
    if get_workspace_isolated():
        return ISOLATED_REJECT_MSG + ": ide_run_terminal"
    if not str(command or "").strip():
        return "执行失败: command 为空"
    res = _ide_bridge("/run_terminal", {"command": command},
                      timeout=max(10, int(timeout or 120)) + 10)
    if not res.get("ok"):
        return "执行失败: %s" % res.get("error")
    out = str(res.get("output") or "")
    code = res.get("exitCode")
    return "%s\nexit=%s\n%s" % (res.get("detail") or "已执行", code, out[-8000:])


def ide_diagnostics(path: str = "") -> str:
    """打开指定文件并读取诊断信息；不传 path 时读取当前活动文件。"""
    note = ""
    if path:
        full, note = scope_path(path)
        opened = _ide_bridge("/open_file", {"path": full})
        if not opened.get("ok"):
            return "读取诊断失败: %s" % opened.get("error")
    ctx = _ide_bridge("/context")
    if not ctx.get("ok"):
        return "读取诊断失败: %s" % ctx.get("error")
    diags = ctx.get("diagnostics") or []
    head = "%s当前文件: %s\n" % (note, ctx.get("file") or "（无）")
    if not diags:
        return head + "没有错误或警告。"
    return head + "\n".join("- %s" % d for d in diags)


def file_list(path: str = ".") -> str:
    """列出目录内容"""
    try:
        _scoped, _scope_note = scope_path(path)
        p = Path(_scoped)
        if not p.exists():
            return _scope_note + f"路径不存在: {_scoped}"
        if not p.is_dir():
            return _scope_note + f"不是目录: {_scoped}"
        items = []
        for item in sorted(p.iterdir()):
            if item.is_dir():
                items.append(f"[DIR] {item.name}/")
            else:
                size = item.stat().st_size
                if size > 1024 * 1024:
                    size_str = f"{size/1024/1024:.1f}MB"
                elif size > 1024:
                    size_str = f"{size/1024:.1f}KB"
                else:
                    size_str = f"{size}B"
                items.append(f"[FILE] {item.name} ({size_str})")
        total = len(items)
        if total > 60:
            # 超长清单不整份交给模型：7B 模型会照着复述全部条目，既慢又容易复读
            return (f"目录: {p.absolute()}（共 {total} 项，仅列前 60 项，"
                    f"需要找特定文件请用 file_search）\n" + "\n".join(items[:60]))
        return f"目录: {p.absolute()}\n" + "\n".join(items)
    except Exception as e:
        return f"列目录错误: {str(e)}"


def file_copy(src: str, dst: str) -> str:
    """复制文件或目录"""
    try:
        _s, _ns = scope_path(src)
        _d, _nd = scope_path(dst)
        shutil.copy2(_s, _d)
        return _ns + _nd + f"已复制: {_s} -> {_d}"
    except IsADirectoryError:
        shutil.copytree(_s, _d)
        return _ns + _nd + f"已复制目录: {_s} -> {_d}"
    except Exception as e:
        return f"复制错误: {str(e)}"


def file_move(src: str, dst: str) -> str:
    """移动/重命名文件或目录"""
    try:
        _s, _ns = scope_path(src)
        _d, _nd = scope_path(dst)
        shutil.move(_s, _d)
        return _ns + _nd + f"已移动: {_s} -> {_d}"
    except Exception as e:
        return f"移动错误: {str(e)}"


def file_delete(path: str) -> str:
    """删除文件或目录"""
    try:
        _scoped, _scope_note = scope_path(path)
        p = Path(_scoped)
        if p.is_dir():
            shutil.rmtree(p)
            return _scope_note + f"已删除目录: {_scoped}"
        else:
            p.unlink()
            return _scope_note + f"已删除文件: {_scoped}"
    except Exception as e:
        return f"删除错误: {str(e)}"


def file_search(directory: str, pattern: str) -> str:
    """搜索文件"""
    try:
        import fnmatch
        directory, _scope_note = scope_path(directory)
        matches = []
        pat = str(pattern or "").strip().lower()
        use_glob = ("*" in pat) or ("?" in pat)
        skip = {'.git', 'node_modules', '__pycache__', 'train_venv', '.venv', 'dist', 'out'}
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if d.lower() not in skip]
            for f in files:
                fl = f.lower()
                hit = fnmatch.fnmatch(fl, pat) if use_glob else (pat in fl)
                if hit:
                    matches.append(os.path.join(root, f))
                    if len(matches) >= 50:
                        break
            if len(matches) >= 50:
                break
        if not matches:
            return _scope_note + f"未找到匹配 '{pattern}' 的文件"
        return _scope_note + f"找到 {len(matches)} 个匹配文件:\n" + "\n".join(matches[:50])
    except Exception as e:
        return f"搜索错误: {str(e)}"


# ============ 代码执行 ============

def run_python(code: str, timeout: int = 60) -> str:
    """运行 Python 代码"""
    if get_workspace_isolated():
        return ISOLATED_REJECT_MSG + ": run_python"
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(code)
            tmp_path = f.name
        result = subprocess.run(
            [sys.executable, tmp_path], capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace"
        )
        os.unlink(tmp_path)
        output = result.stdout
        if result.stderr:
            output += "\n[stderr]\n" + result.stderr
        return output.strip() if output.strip() else "(代码执行完毕，无输出)"
    except subprocess.TimeoutExpired:
        return f"代码执行超时（{timeout}秒）"
    except Exception as e:
        return f"执行错误: {str(e)}"


# ============ 鼠标控制 ============

def mouse_move(x: int, y: int) -> str:
    """移动鼠标到坐标(x, y)"""
    if not HAS_PYAUTOGUI:
        return "pyautogui 不可用"
    try:
        pyautogui.moveTo(x, y, duration=0.2)
        return f"鼠标已移动到 ({x}, {y})"
    except Exception as e:
        return f"鼠标移动错误: {str(e)}"


def mouse_click(x: int = None, y: int = None, button: str = "left", clicks: int = 1) -> str:
    """鼠标点击。不传坐标则点击当前位置"""
    if not HAS_PYAUTOGUI:
        return "pyautogui 不可用"
    try:
        if x is not None and y is not None:
            pyautogui.click(x, y, clicks=clicks, button=button, duration=0.1)
            return f"在 ({x}, {y}) {button}键点击 {clicks}次"
        else:
            pyautogui.click(clicks=clicks, button=button)
            return f"当前位置 {button}键点击 {clicks}次"
    except Exception as e:
        return f"鼠标点击错误: {str(e)}"


def mouse_double_click(x: int = None, y: int = None) -> str:
    """鼠标双击"""
    if not HAS_PYAUTOGUI:
        return "pyautogui 不可用"
    try:
        if x is not None and y is not None:
            pyautogui.doubleClick(x, y)
            return f"在 ({x}, {y}) 双击"
        else:
            pyautogui.doubleClick()
            return "当前位置双击"
    except Exception as e:
            return f"鼠标双击错误: {str(e)}"


def mouse_right_click(x: int = None, y: int = None) -> str:
    """鼠标右键点击"""
    if not HAS_PYAUTOGUI:
        return "pyautogui 不可用"
    try:
        if x is not None and y is not None:
            pyautogui.rightClick(x, y)
            return f"在 ({x}, {y}) 右键点击"
        else:
            pyautogui.rightClick()
            return "当前位置右键点击"
    except Exception as e:
        return f"右键点击错误: {str(e)}"


def mouse_drag(x1: int, y1: int, x2: int, y2: int) -> str:
    """从(x1,y1)拖拽到(x2,y2)"""
    if not HAS_PYAUTOGUI:
        return "pyautogui 不可用"
    try:
        pyautogui.moveTo(x1, y1, duration=0.2)
        pyautogui.dragTo(x2, y2, duration=0.5, button="left")
        return f"已从 ({x1}, {y1}) 拖拽到 ({x2}, {y2})"
    except Exception as e:
        return f"拖拽错误: {str(e)}"


def mouse_scroll(amount: int) -> str:
    """滚动鼠标滚轮。正数向上，负数向下"""
    if not HAS_PYAUTOGUI:
        return "pyautogui 不可用"
    try:
        pyautogui.scroll(amount)
        return f"已滚动 {'上' if amount > 0 else '下'} {abs(amount)} 格"
    except Exception as e:
        return f"滚动错误: {str(e)}"


def mouse_position() -> str:
    """获取当前鼠标位置"""
    if not HAS_PYAUTOGUI:
        return "pyautogui 不可用"
    try:
        x, y = pyautogui.position()
        return f"当前鼠标位置: ({x}, {y})"
    except Exception as e:
        return f"获取位置错误: {str(e)}"


# ============ 键盘控制 ============

def key_type(text: str) -> str:
    """输入文本"""
    if not HAS_PYAUTOGUI:
        return "pyautogui 不可用"
    try:
        pyautogui.typewrite(text, interval=0.02) if text.isascii() else pyautogui.write(text)
        return f"已输入文本: {text[:50]}..."
    except Exception as e:
        return f"输入错误: {str(e)}"


def key_press(keys: str) -> str:
    """按键。支持组合键如 'ctrl+c'"""
    if not HAS_PYAUTOGUI:
        return "pyautogui 不可用"
    try:
        key_list = [k.strip() for k in keys.split("+")]
        if len(key_list) == 1:
            pyautogui.press(key_list[0])
            return f"已按键: {keys}"
        else:
            pyautogui.hotkey(*key_list)
            return f"已组合键: {keys}"
    except Exception as e:
        return f"按键错误: {str(e)}"


def key_hold(key: str, duration: float = 1.0) -> str:
    """按住键指定时间"""
    if not HAS_PYAUTOGUI:
        return "pyautogui 不可用"
    try:
        pyautogui.keyDown(key)
        time.sleep(duration)
        pyautogui.keyUp(key)
        return f"已按住 {key} {duration}秒"
    except Exception as e:
        return f"按住错误: {str(e)}"


# ============ 截图 ============

def screenshot(filename: str = None) -> str:
    """截取屏幕，保存为文件"""
    if not HAS_PIL:
        return "PIL 不可用"
    try:
        if not filename:
            filename = os.path.join(tempfile.gettempdir(), f"screenshot_{int(time.time())}.png")
        img = ImageGrab.grab()
        img.save(filename)
        return f"截图已保存: {filename} ({img.size[0]}x{img.size[1]})"
    except Exception as e:
        return f"截图错误: {str(e)}"


# ============ 剪贴板 ============

def clipboard_read() -> str:
    """读取剪贴板内容"""
    if not HAS_CLIPBOARD:
        return "pyperclip 不可用"
    try:
        content = pyperclip.paste()
        return f"剪贴板内容: {content[:500]}"
    except Exception as e:
        return f"读取剪贴板错误: {str(e)}"


def clipboard_write(text: str) -> str:
    """写入剪贴板"""
    if not HAS_CLIPBOARD:
        return "pyperclip 不可用"
    try:
        pyperclip.copy(text)
        return f"已写入剪贴板: {text[:50]}..."
    except Exception as e:
        return f"写入剪贴板错误: {str(e)}"


# ============ 进程管理 ============

def process_list() -> str:
    """列出所有运行中的进程"""
    if not HAS_PSUTIL:
        return "psutil 不可用"
    try:
        procs = []
        for p in psutil.process_iter(['pid', 'name', 'memory_info']):
            try:
                mem = p.info['memory_info'].rss / 1024 / 1024 if p.info['memory_info'] else 0
                procs.append(f"PID:{p.info['pid']:>6}  MEM:{mem:>8.1f}MB  {p.info['name']}")
            except Exception:
                pass
        procs.sort(key=lambda x: int(x.split('MEM:')[1].split('M')[0]), reverse=True)
        return f"共 {len(procs)} 个进程（按内存排序）:\n" + "\n".join(procs[:30])
    except Exception as e:
        return f"进程列表错误: {str(e)}"


def process_kill(pid: int, name: str = "") -> str:
    """终止进程"""
    if get_workspace_isolated():
        return ISOLATED_REJECT_MSG + ": process_kill"
    if not HAS_PSUTIL:
        return "psutil 不可用"
    try:
        p = psutil.Process(int(pid))
        name = p.name()
        p.terminate()
        return f"已终止进程: {name} (PID:{pid})"
    except psutil.NoSuchProcess:
        return f"进程不存在: PID {pid}"
    except Exception as e:
        return f"终止进程错误: {str(e)}"


def process_start(command: str) -> str:
    """启动新进程"""
    if get_workspace_isolated():
        return ISOLATED_REJECT_MSG + ": process_start"
    try:
        subprocess.Popen(command, shell=True, creationflags=_NO_WINDOW)
        return f"已启动进程: {command}"
    except Exception as e:
        return f"启动进程错误: {str(e)}"


# ============ 窗口管理 ============

def window_list() -> str:
    """列出所有窗口"""
    try:
        import pygetwindow as gw
        windows = gw.getAllWindows()
        result = []
        for w in windows:
            if w.title:
                result.append(f"句柄:{w._hWnd}  标题:{w.title}  位置:({w.left},{w.top}) 大小:{w.width}x{w.height}")
        return f"共 {len(result)} 个窗口:\n" + "\n".join(result[:20])
    except Exception as e:
        return f"窗口列表错误: {str(e)}"


def window_activate(title: str) -> str:
    """激活窗口（置顶）"""
    try:
        import pygetwindow as gw
        windows = gw.getWindowsWithTitle(title)
        if not windows:
            return f"未找到标题包含 '{title}' 的窗口"
        w = windows[0]
        if w.isMinimized:
            w.restore()
        w.activate()
        return f"已激活窗口: {w.title}"
    except Exception as e:
        return f"激活窗口错误: {str(e)}"


def window_minimize(title: str) -> str:
    """最小化窗口"""
    try:
        import pygetwindow as gw
        windows = gw.getWindowsWithTitle(title)
        if windows:
            windows[0].minimize()
            return f"已最小化: {windows[0].title}"
        return f"未找到窗口: {title}"
    except Exception as e:
        return f"最小化错误: {str(e)}"


def window_maximize(title: str) -> str:
    """最大化窗口"""
    try:
        import pygetwindow as gw
        windows = gw.getWindowsWithTitle(title)
        if windows:
            windows[0].maximize()
            return f"已最大化: {windows[0].title}"
        return f"未找到窗口: {title}"
    except Exception as e:
        return f"最大化错误: {str(e)}"


def window_close(title: str) -> str:
    """关闭窗口"""
    try:
        import pygetwindow as gw
        windows = gw.getWindowsWithTitle(title)
        if windows:
            windows[0].close()
            return f"已关闭窗口: {windows[0].title}"
        return f"未找到窗口: {title}"
    except Exception as e:
        return f"关闭窗口错误: {str(e)}"


# ============ 应用控制 ============

def open_app(app_name: str) -> str:
    """打开应用程序"""
    try:
        app_map = {
            "notepad": "notepad", "记事本": "notepad",
            "calc": "calc", "计算器": "calc",
            "explorer": "explorer", "文件管理器": "explorer",
            "cmd": "cmd", "命令行": "cmd", "终端": "cmd",
            "paint": "mspaint", "画图": "mspaint",
            "browser": "start chrome", "浏览器": "start chrome",
            "chrome": "start chrome", "edge": "start msedge",
            "firefox": "start firefox",
            "vscode": "code", "代码编辑器": "code",
            "word": "start winword", "excel": "start excel",
            "powerpoint": "start powerpnt", "ppt": "start powerpnt",
            "taskmgr": "taskmgr", "任务管理器": "taskmgr",
            "control": "control", "控制面板": "control",
            "settings": "start ms-settings:", "设置": "start ms-settings:",
            "regedit": "regedit", "注册表": "regedit",
            "services": "services.msc", "服务": "services.msc",
        }
        cmd = app_map.get(app_name.lower(), app_name)
        subprocess.Popen(cmd, shell=True, creationflags=_NO_WINDOW)
        return f"已打开: {app_name}"
    except Exception as e:
        return f"打开失败: {str(e)}"


# ============ 系统控制 ============

def system_control(action: str) -> str:
    """系统控制操作"""
    if get_workspace_isolated():
        return ISOLATED_REJECT_MSG + ": system_control"
    try:
        actions = {
            "screenshot": ("ms-screenclip:", "已打开截图工具"),
            "lock": ("rundll32.exe user32.dll,LockWorkStation", "正在锁屏..."),
            "volume_up": ('powershell -c "(New-Object -ComObject WScript.Shell).SendKeys([char]175)"', "音量已增加"),
            "volume_down": ('powershell -c "(New-Object -ComObject WScript.Shell).SendKeys([char]174)"', "音量已减少"),
            "mute": ('powershell -c "(New-Object -ComObject WScript.Shell).SendKeys([char]173)"', "静音切换"),
            "shutdown": ("shutdown /s /t 30", "系统将在30秒后关机（用 shutdown /a 取消）"),
            "restart": ("shutdown /r /t 30", "系统将在30秒后重启（用 shutdown /a 取消）"),
            "sleep": ('rundll32.exe powrprof.dll,SetSuspendState 0,1,0', "正在进入睡眠..."),
            "hibernate": ("shutdown /h", "正在休眠..."),
            "logoff": ("shutdown /l", "正在注销..."),
            "cancel_shutdown": ("shutdown /a", "已取消关机/重启计划"),
            "empty_recycle_bin": ('powershell -c "Clear-RecycleBin -Force"', "已清空回收站"),
            "clear_temp": ('del /q /f "%TEMP%\\*"', "已清理临时文件"),
            "flush_dns": ("ipconfig /flushdns", "已刷新DNS缓存"),
        }
        if action in actions:
            cmd, msg = actions[action]
            subprocess.Popen(cmd, shell=True, creationflags=_NO_WINDOW)
            return msg
        return f"未知操作: {action}"
    except Exception as e:
        return f"操作失败: {str(e)}"


# ============ 系统信息 ============

def system_info() -> str:
    """获取系统信息"""
    try:
        import psutil
        import platform as pf
        info = []
        info.append(f"系统: {pf.system()} {pf.version()}")
        info.append(f"计算机名: {pf.node()}")
        info.append(f"处理器: {pf.processor()}")
        info.append(f"CPU核心: {psutil.cpu_count(logical=True)} 逻辑 / {psutil.cpu_count(logical=False)} 物理")
        info.append(f"CPU使用率: {psutil.cpu_percent(interval=1)}%")
        mem = psutil.virtual_memory()
        info.append(f"内存: {mem.total/1024/1024/1024:.1f}GB 总共, {mem.available/1024/1024/1024:.1f}GB 可用, {mem.percent}% 使用")
        disk = psutil.disk_usage('C:')
        info.append(f"C盘: {disk.total/1024/1024/1024:.1f}GB 总共, {disk.free/1024/1024/1024:.1f}GB 可用, {disk.percent}% 使用")
        info.append(f"Python: {pf.python_version()}")
        net = psutil.net_if_addrs()
        for name, addrs in net.items():
            for addr in addrs:
                if addr.family.name == 'AF_INET':
                    info.append(f"网络 {name}: {addr.address}")
        boot_time = psutil.boot_time()
        uptime = time.time() - boot_time
        info.append(f"开机时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(boot_time))}")
        info.append(f"运行时长: {int(uptime//3600)}小时{int((uptime%3600)//60)}分钟")
        return "\n".join(info)
    except Exception as e:
        return f"获取系统信息错误: {str(e)}"


# ============ 网络操作 ============

def network_request(url: str, method: str = "GET", data: str = None, timeout: int = 30) -> str:
    """发送 HTTP 请求"""
    try:
        import urllib.request
        req = urllib.request.Request(url, method=method)
        if data:
            req.data = data.encode("utf-8")
            req.add_header("Content-Type", "application/json")
        resp = urllib.request.urlopen(req, timeout=timeout)
        body = resp.read().decode("utf-8", errors="replace")
        return f"状态: {resp.status}\n{body[:2000]}"
    except Exception as e:
        return f"网络请求错误: {str(e)}"


def network_download(url: str, path: str) -> str:
    """下载文件"""
    try:
        import urllib.request
        urllib.request.urlretrieve(url, path)
        size = os.path.getsize(path)
        return f"已下载: {url} -> {path} ({size/1024:.1f}KB)"
    except Exception as e:
        return f"下载错误: {str(e)}"


def network_ping(host: str, count: int = 4) -> str:
    """ping 主机"""
    try:
        result = subprocess.run(
            f"ping -n {count} {host}", shell=True, capture_output=True,
            text=True, timeout=30, encoding="utf-8", errors="replace"
        )
        return result.stdout or "(无输出)"
    except Exception as e:
        return f"Ping错误: {str(e)}"


# ============ 环境变量 ============

def env_get(name: str) -> str:
    """获取环境变量"""
    val = os.environ.get(name)
    return f"{name}={val}" if val else f"环境变量 {name} 未设置"


def env_set(name: str, value: str) -> str:
    """设置环境变量（永久）"""
    if get_workspace_isolated():
        return ISOLATED_REJECT_MSG + ": env_set"
    try:
        subprocess.run(
            ["powershell", "-Command", f"[Environment]::SetEnvironmentVariable('{name}', '{value}', 'User')"],
            capture_output=True, timeout=10
        )
        os.environ[name] = value
        return f"已设置环境变量: {name}={value}"
    except Exception as e:
        return f"设置环境变量错误: {str(e)}"


def env_list() -> str:
    """列出所有环境变量"""
    return "\n".join(f"{k}={v}" for k, v in sorted(os.environ.items()))


# ============ 注册表 ============

def registry_read(key_path: str, value_name: str = "") -> str:
    """读取注册表"""
    try:
        ps_cmd = f"Get-ItemProperty -Path 'Registry::{key_path}'"
        if value_name:
            ps_cmd += f" | Select-Object -ExpandProperty {value_name}"
        result = subprocess.run(
            ["powershell", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace"
        )
        return result.stdout.strip() or "(未找到)"
    except Exception as e:
        return f"读取注册表错误: {str(e)}"


def registry_write(key_path: str, value_name: str, value: str, type: str = "String") -> str:
    """写入注册表"""
    if get_workspace_isolated():
        return ISOLATED_REJECT_MSG + ": registry_write"
    try:
        ps_cmd = f"New-Item -Path 'Registry::{key_path}' -Force; Set-ItemProperty -Path 'Registry::{key_path}' -Name '{value_name}' -Value '{value}' -Type {type}"
        subprocess.run(["powershell", "-Command", ps_cmd], capture_output=True, timeout=10)
        return f"已写入注册表: {key_path}\\{value_name}={value}"
    except Exception as e:
        return f"写入注册表错误: {str(e)}"


# ============ 服务管理 ============

def service_list() -> str:
    """列出系统服务"""
    try:
        result = subprocess.run(
            ["powershell", "-Command", "Get-Service | Select-Object Status,Name,DisplayName | Format-Table -AutoSize"],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace"
        )
        return result.stdout[:3000] or "(无服务)"
    except Exception as e:
        return f"服务列表错误: {str(e)}"


def service_control(name: str, action: str) -> str:
    """控制服务（start/stop/restart）"""
    if get_workspace_isolated():
        return ISOLATED_REJECT_MSG + ": service_control"
    try:
        ps_cmd = f"{action}-Service -Name '{name}'"
        subprocess.run(["powershell", "-Command", ps_cmd], capture_output=True, timeout=30)
        action_map = {"start": "启动", "stop": "停止", "restart": "重启"}
        return f"已{action_map.get(action, action)}服务: {name}"
    except Exception as e:
        return f"服务控制错误: {str(e)}"


# ============ 自我进化：持久化记忆（Fernet+DPAPI 加密存储） ============
import secure_store as _secure

MEMORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "learnings.jsonl")
_memory_lock = __import__("threading").Lock()


def _load_memories():
    """读取全部记忆（自动从旧明文 learnings.jsonl 迁移），返回 [{id, time, tag, content}]"""
    if os.path.exists(MEMORY_PATH):
        _secure.migrate_plaintext(MEMORY_PATH, _secure.MEMORY_ENC)
    return _secure.MEMORY_ENC.load_all()


def memory_save(content: str, tag: str = "") -> str:
    """持久化记住一条内容，以后每次对话自动注入"""
    content = (content or "").strip()
    if not content:
        return "记忆内容为空，未保存"
    with _memory_lock:
        items = _load_memories()
        # 去重：完全相同的内容不重复保存
        if any(it.get("content") == content for it in items):
            return "这条我已经记住了，无需重复"
        mid = (max((it.get("id", 0) for it in items), default=0) + 1)
        _secure.MEMORY_ENC.append({
            "id": mid,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tag": tag or "知识",
            "content": content,
        })
    return f"已永久记住(#{mid})，以后所有对话我都会遵守/应用：{content}"


def memory_list() -> str:
    """列出所有记忆"""
    items = _load_memories()
    if not items:
        return "目前还没有记住任何内容（共0条）"
    lines = [f"已记住 {len(items)} 条："]
    for it in items:
        lines.append(f"#{it['id']} [{it.get('tag','')}] {it['content']}")
    return "\n".join(lines)


def memory_forget(memory_id) -> str:
    """按编号删除记忆"""
    try:
        mid = int(memory_id)
    except (TypeError, ValueError):
        return "memory_id 必须是数字编号"
    with _memory_lock:
        removed = _secure.MEMORY_ENC.delete_ids([mid])
        if not removed:
            return f"没有找到 #{mid}"
    return f"已删除记忆 #{mid}"


# ============ 跨会话任务追踪（Agent Task Tracker）============
_TASK_LOCK = __import__("threading").Lock()
_TASKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_model_cache", "tasks")


def _tasks_store():
    os.makedirs(_TASKS_DIR, exist_ok=True)
    return os.path.join(_TASKS_DIR, "active.json")


def _load_tasks():
    fp = _tasks_store()
    if not os.path.exists(fp):
        return []
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_tasks(items):
    with open(_tasks_store(), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def task_save(title: str, steps: str = "", goal: str = "") -> str:
    """持久化一个待办任务，跨会话可追踪、可续跑"""
    title = (title or "").strip()
    if not title:
        return "任务标题不能为空"
    steps = (steps or "").strip()
    goal = (goal or "").strip()
    with _TASK_LOCK:
        items = _load_tasks()
        tid = (max((t.get("id", 0) for t in items), default=0) + 1)
        items.insert(0, {
            "id": tid, "title": title, "steps": steps, "goal": goal,
            "status": "pending",  # pending / in_progress / done / blocked
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "session_id": "",
        })
        _save_tasks(items)
    return f"任务已保存 #{tid}：{title}（steps={steps[:60]}{'...' if len(steps) > 60 else ''}）"


def task_list() -> str:
    """列出所有未完成任务"""
    items = _load_tasks()
    pending = [t for t in items if t.get("status") != "done"]
    if not pending:
        return "目前没有待办任务（共 0 条）"
    lines = [f"待办任务 {len(pending)} 条："]
    for t in pending:
        lines.append(f"  #{t['id']} [{t.get('status','pending')}] {t['title']}")
        if t.get("steps"):
            lines.append(f"      steps: {t['steps'][:120]}")
    return "\n".join(lines)


def task_update(task_id, status: str = "", progress: str = "") -> str:
    """更新任务状态或进度"""
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return "task_id 必须是数字"
    with _TASK_LOCK:
        items = _load_tasks()
        for t in items:
            if t.get("id") == tid:
                if status:
                    t["status"] = status
                if progress:
                    t["steps"] = (t.get("steps", "") + "\n---\n" + progress).strip()
                t["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _save_tasks(items)
                return f"已更新 #{tid} status={status or t.get('status')}"
        return f"未找到任务 #{tid}"


def task_close(task_id, summary: str = "") -> str:
    """标记任务完成（归档，不再列出）"""
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return "task_id 必须是数字"
    with _TASK_LOCK:
        items = _load_tasks()
        for t in items:
            if t.get("id") == tid:
                t["status"] = "done"
                if summary:
                    t["steps"] = (t.get("steps", "") + "\nDONE: " + summary).strip()
                t["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _save_tasks(items)
                return f"已关闭任务 #{tid}：{t['title']}"
    return f"未找到任务 #{tid}"


def task_resume() -> str:
    """自动续跑：列出 pending 任务，让 LLM 选择一个继续执行"""
    items = _load_tasks()
    pending = [t for t in items if t.get("status") != "done"]
    if not pending:
        return "✅ 目前没有待办任务，所有任务都已完成！"
    # 把待办任务格式化为 LLM 能直接消费的列表
    lines = ["📋 当前待办任务（按优先级）："]
    for t in pending:
        lines.append(f"\n[#{t['id']}] {t['title']}")
        if t.get("goal"):
            lines.append(f"    🎯 goal: {t['goal'][:200]}")
        if t.get("steps"):
            lines.append(f"    📝 steps: {t['steps'][:200]}")
        lines.append(f"    🔧 status: {t.get('status','pending')}  📅 created: {t.get('created')}")
    lines.append("\n请选择一个任务开始执行，用 task_update(id, status='in_progress') 标记开始，完成后用 task_close(id, summary) 归档。")
    return "\n".join(lines)


# ============ 自我训练 ============
def train_start(goal: str, rounds=10, resume=False) -> str:
    """启动自我训练：双角色自对话 → 评分 → 高分知识写入记忆"""
    if not goal or not str(goal).strip():
        return "需要提供训练主题 goal"
    import self_train
    ok, msg = self_train.start_training(goal=str(goal).strip(),
                                        rounds=int(rounds) if rounds else None,
                                        resume=bool(resume))
    return msg if ok else f"启动失败: {msg}"


def train_status() -> str:
    """查看自我训练当前进度与状态"""
    import self_train
    st = self_train.get_status()
    if not st.get("running") and not st.get("goal"):
        return "当前没有训练任务。可用 train_start(goal, rounds) 启动自我训练"
    parts = [f"状态: {'训练中' if st.get('running') else '已停止'}",
             f"主题: {st.get('goal')}",
             f"轮次: {st.get('round')}/{st.get('rounds')}",
             f"已学知识: {st.get('learned_count')}条",
             f"进度: {st.get('progress_pct', 0)}%"]
    log_tail = st.get("log") or []
    if log_tail:
        parts.append("最近: " + " | ".join(log_tail[-3:]))
    return "\n".join(parts)


def train_stop() -> str:
    """停止当前自我训练任务（已学知识保留，支持断点续训）"""
    import self_train
    ok = self_train.stop_training()
    return "停止指令已发出，进度已存档" if ok else "停止失败"


# ============ 局域网设备控制 ============
def device_scan(timeout=3) -> str:
    """扫描局域网设备：SSDP发现 + Ping/ARP 存活识别（结果缓存）"""
    if not _cfg.get("devices.enabled", True):
        return "设备控制功能已在 config.json 中禁用(devices.enabled=false)"
    try:
        import devices
        n = devices.scan_devices(timeout=float(timeout) if timeout else None)
        return f"扫描完成，共发现 {n} 台设备。可调用 device_list 查看清单"
    except Exception as e:
        return f"设备扫描失败: {e}"


def device_list() -> str:
    """列出已发现设备清单（id/类型/IP/端口/在线状态）"""
    try:
        import devices
        return devices.format_devices_text()
    except Exception as e:
        return f"读取设备清单失败: {e}"


def device_control(device_id, action, value=None) -> str:
    """控制设备：action=on/off/toggle/brightness/volume/mode 等"""
    if not _cfg.get("devices.enabled", True):
        return "设备控制功能已在 config.json 中禁用(devices.enabled=false)"
    try:
        import devices
        ok, msg = devices.control_device(str(device_id), str(action), value)
        return msg
    except Exception as e:
        return f"设备控制失败: {e}"


# ============ 推理闭环工具（思考笔记本 + 任务终止信号） ============

def sequential_thinking(thought: str, step: str = "", total_steps: str = "") -> str:
    """结构化思考工具：把当前推理步骤记录到「思考笔记本」。
    模型在动手执行任何工具前必须先调用一次，声明当前在第几步、总共几步、以及这一步的推理内容。
    服务端会把 thought 记入对话轨迹的 think 段，用于界面折叠展示。"""
    step_s = str(step) if step else "?"
    total_s = str(total_steps) if total_steps else "?"
    return f"[思考已记录] 第 {step_s}/{total_s} 步：{str(thought)[:200]}"


def task_done(summary: str = "") -> str:
    """任务完成信号：模型判定全部步骤执行完毕、结果已验证后调用，
    服务端收到后立即终止工具调用循环，把 summary 作为最终交付总结返回给用户。"""
    return f"[任务完成] {str(summary)}"


def _code_read(path: str, start_line: int = None, end_line: int = None) -> str:
    """code_read 的实现：读取文件并按行号范围截取（复用统一读取层，兼容二进制探测）"""
    try:
        off = max(0, int(start_line) - 1) if start_line else 0
        lim = max(0, int(end_line) - off) if end_line else 0
        return readers.retag(file_read(path, offset=off, limit=lim), "code_read")
    except Exception as e:
        return readers.fail("code_read", "INVALID_ARG", f"读取错误: {e}",
                            "start_line/end_line 用整数，从 1 开始计数")


def _code_search(query: str, file_pattern: str = "*") -> str:
    """code_search 的实现：在当前工作目录按关键词搜索文件"""
    return file_search(".", query)


def _run_code(code: str) -> str:
    """run_code 的实现：执行 Python 代码"""
    return run_python(code)


def _apply_patch(path: str, patch: str) -> str:
    """apply_patch 的实现：按 hunk 应用 unified diff（支持上下文匹配与删除行）。

    旧实现只保留 '+' 行、丢弃 '-' 行，导致删除不生效且上下文错位。
    这里解析每个 @@ hunk，按 old 块（上下文+删除行）在原文中定位后整体替换为 new 块。
    """
    import re
    try:
        with open(path, "r", encoding="utf-8") as f:
            orig_lines = f.read().splitlines(keepends=True)
    except FileNotFoundError:
        orig_lines = []

    # 1) 解析 patch 为 hunks：old = 上下文+删除行，new = 上下文+新增行
    hunks = []
    cur = None
    for line in patch.splitlines():
        if line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if not m:
                return f"补丁应用失败: 无法解析 hunk 头 {line!r}"
            cur = {"old_start": int(m.group(1)), "old": [], "new": []}
            hunks.append(cur)
        elif line.startswith("---") or line.startswith("+++"):
            continue
        elif cur is not None:
            if line.startswith("-"):
                cur["old"].append(line[1:])
            elif line.startswith("+"):
                cur["new"].append(line[1:])
            elif line.startswith(" ") or line == "":
                ctx = line[1:] if line.startswith(" ") else ""
                cur["old"].append(ctx)
                cur["new"].append(ctx)
            # 其余行（如 "\ No newline at end of file"）忽略
    if not hunks:
        return "补丁应用失败: 未找到任何 hunk（@@ 段）"

    def norm(s):
        return s.rstrip("\r\n")

    # 2) 逐 hunk 在原文中定位 old 块并替换为 new 块
    result = list(orig_lines)
    offset = 0  # 已应用 hunk 累积的行号偏移
    for h in hunks:
        old, new = h["old"], h["new"]
        declared = h["old_start"] - 1 + offset
        candidates = []
        if 0 <= declared <= len(result) - len(old):
            candidates.append(declared)
        for pos in range(0, len(result) - len(old) + 1):
            if pos != declared:
                candidates.append(pos)
        found = -1
        for pos in candidates:
            if all(norm(result[pos + k]) == norm(old[k]) for k in range(len(old))):
                found = pos
                break
        if found < 0:
            return (f"补丁应用失败: 在 {path} 中找不到 hunk（@@ -{h['old_start']}）"
                    f"对应的原文，补丁可能已过期")
        new_lines = [nl if nl.endswith("\n") else nl + "\n" for nl in new]
        result[found:found + len(old)] = new_lines
        offset += len(new) - len(old)

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(result))
    except Exception as e:
        return f"补丁应用失败: 写入 {path} 出错: {e}"
    return f"补丁已应用到 {path}（{len(hunks)} 个 hunk）"


# ============ 工具注册表 ============

TOOLS = {
    # ============ 自我进化：持久化记忆 ============
    "memory_save": {"description": "记住一条知识/规则/用户偏好（永久生效）；用户说'记住/以后/学会'时必须调用", "parameters": {"content": "要记住的内容", "tag": "分类标签,如偏好/规则/知识(可选)"}, "function": lambda content, tag="": memory_save(content, tag)},
    "memory_list": {"description": "列出所有已记住的知识", "parameters": {}, "function": memory_list},
    "memory_forget": {"description": "删除某条记忆", "parameters": {"memory_id": "记忆编号"}, "function": memory_forget},
    # ============ 跨会话任务追踪 ============
    "task_save": {"description": "保存一个待办任务（跨会话持久化，下次对话自动续跑）。复杂多步骤任务开始前先存档", "parameters": {"title": "任务标题", "steps": "当前已完成的步骤/进度(可选)", "goal": "最终目标(可选)"}, "function": lambda title, steps="", goal="": task_save(title, steps, goal)},
    "task_list": {"description": "列出所有未完成的待办任务", "parameters": {}, "function": task_list},
    "task_update": {"description": "更新任务状态或进度", "parameters": {"task_id": "任务编号", "status": "新状态: pending/in_progress/done/blocked", "progress": "进度补充描述(可选)"}, "function": lambda task_id, status="", progress="": task_update(task_id, status, progress)},
    "task_close": {"description": "标记任务完成（归档）", "parameters": {"task_id": "任务编号", "summary": "完成总结(可选)"}, "function": lambda task_id, summary="": task_close(task_id, summary)},
    "task_resume": {"description": "自动续跑：列出所有 pending 任务，展示完整上下文，推荐优先执行哪个", "parameters": {}, "function": task_resume},
    # ============ 子智能体（独立上下文；agent_delegate 的实际执行在 api_server）============
    "agent_delegate": {"description": "把边界清晰的检索/分析任务委派给子智能体执行，它拥有独立上下文、只返回结论。适合需要读大量文件才能得出结论的场景", "parameters": {"name": "子智能体名称（先用 agent_list 查看）", "task": "要它完成的具体任务，必须自带背景信息（子智能体看不到当前对话）"}, "function": lambda name, task="": _agent_delegate_stub(name, task)},
    "agent_list": {"description": "列出可用的子智能体及其用途和工具权限", "parameters": {}, "function": lambda: _agents_list_text()},
    # ============ 自我训练 ============
    "train_start": {"description": "启动自我训练：围绕一个主题双角色自对话学习，高分知识自动写入记忆(永久生效)。训练耗时长，适合用户说'自我训练/学习XX主题/进化'时调用", "parameters": {"goal": "训练主题，如'Windows使用技巧'", "rounds": "训练轮数(默认10,可选)", "resume": "是否从上次断点继续(true/false,可选)"}, "function": lambda goal, rounds=10, resume=False: train_start(goal, rounds, resume)},
    "train_status": {"description": "查看自我训练的当前进度（轮次/已学知识/日志）", "parameters": {}, "function": train_status},
    "train_stop": {"description": "停止当前自我训练（已学知识保留，下次可断点续训）", "parameters": {}, "function": train_stop},
    # 文件操作
    "shell_exec": {
        "description": "执行 Windows shell 命令",
        "parameters": {"command": "命令", "timeout": "超时秒数(可选)", "cwd": "项目内工作目录(可选)"},
        "function": shell_exec,
    },
    "shell_admin": {
        "description": "以管理员权限执行命令",
        "parameters": {"command": "命令"},
        "function": shell_admin,
    },
    "file_read": {"description": "读取文本/代码（offset/limit 分段）；自动路由 PDF/Office/图片，二进制只返回类型信息", "parameters": {"path": "路径", "offset": "起始行(从0计,可选)", "limit": "最多行数(可选)", "max_chars": "最大字符数(可选)"}, "function": file_read},
    # ============ IDE 桥：读缓冲区 / 改编辑器 / 跑终端 / 看诊断 ============
    "ide_open_file": {"description": "在 IDE 编辑器中打开文件并跳到指定行（改代码前先打开目标文件）", "parameters": {"path": "文件路径", "line": "行号(可选,从1计)"}, "function": lambda path, line=0: ide_open_file(path, line)},
    "ide_read_file": {"description": "读取 IDE 编辑器缓冲区（含未保存修改），按行号返回；适合改代码前精确查看", "parameters": {"path": "文件路径", "offset": "起始行(从1计,可选)", "limit": "最多行数(默认400)"}, "function": lambda path, offset=1, limit=0: ide_read_file(path, offset, limit)},
    "ide_edit_file": {"description": "通过 IDE WorkspaceEdit 修改并保存：old_string 为空=全文替换；保留撤销栈", "parameters": {"path": "文件路径", "old_string": "被替换的原文(可选)", "new_string": "替换后的内容(可选)", "content": "全文内容(可选,等价于 old_string 为空)"}, "function": lambda path, old_string="", new_string="", content="": ide_edit_file(path, old_string, new_string, content)},
    "ide_run_terminal": {"description": "在 IDE 的 wasily AI 终端执行命令并回传输出与退出码；用于跑测试/构建/启动", "parameters": {"command": "命令", "timeout": "超时秒数(可选)"}, "function": lambda command, timeout=120: ide_run_terminal(command, timeout)},
    "ide_diagnostics": {"description": "读取当前文件（或指定文件）的编译/语法/类型诊断，改完代码必须调用", "parameters": {"path": "文件路径(可选,不传=当前活动文件)"}, "function": lambda path="": ide_diagnostics(path)},
    "file_write": {"description": "写入文件", "parameters": {"path": "路径", "content": "内容"}, "function": file_write},
    "save_output": {"description": "把最终产物保存到产物库（Agent 中心「我的产物」可见）", "parameters": {"filename": "文件名", "content": "内容"}, "function": save_output},
    "file_list": {"description": "列目录", "parameters": {"path": "路径"}, "function": file_list},
    "file_copy": {"description": "复制文件/目录", "parameters": {"src": "源", "dst": "目标"}, "function": file_copy},
    "file_move": {"description": "移动/重命名", "parameters": {"src": "源", "dst": "目标"}, "function": file_move},
    "file_delete": {"description": "删除文件/目录", "parameters": {"path": "路径"}, "function": file_delete},
    "file_search": {"description": "搜索文件", "parameters": {"directory": "目录", "pattern": "文件名关键词"}, "function": file_search},
    # 代码执行
    "run_python": {"description": "运行Python代码", "parameters": {"code": "代码"}, "function": run_python},
    # 鼠标控制
    "mouse_move": {"description": "移动鼠标", "parameters": {"x": "X坐标", "y": "Y坐标"}, "function": mouse_move},
    "mouse_click": {"description": "鼠标点击", "parameters": {"x": "X(可选)", "y": "Y(可选)", "button": "left/right(可选)", "clicks": "次数(可选)"}, "function": mouse_click},
    "mouse_double_click": {"description": "鼠标双击", "parameters": {"x": "X(可选)", "y": "Y(可选)"}, "function": mouse_double_click},
    "mouse_right_click": {"description": "右键点击", "parameters": {"x": "X(可选)", "y": "Y(可选)"}, "function": mouse_right_click},
    "mouse_drag": {"description": "拖拽", "parameters": {"x1": "起点X", "y1": "起点Y", "x2": "终点X", "y2": "终点Y"}, "function": mouse_drag},
    "mouse_scroll": {"description": "滚轮(正=上,负=下)", "parameters": {"amount": "格数"}, "function": mouse_scroll},
    "mouse_position": {"description": "获取鼠标位置", "parameters": {}, "function": mouse_position},
    # 键盘控制
    "key_type": {"description": "输入文本", "parameters": {"text": "文本"}, "function": key_type},
    "key_press": {"description": "按键/组合键(如ctrl+c)", "parameters": {"keys": "键名"}, "function": key_press},
    "key_hold": {"description": "按住键", "parameters": {"key": "键名", "duration": "秒数"}, "function": key_hold},
    # 截图
    "screenshot": {"description": "截屏保存文件", "parameters": {"filename": "保存路径(可选)"}, "function": screenshot},
    # 剪贴板
    "clipboard_read": {"description": "读剪贴板", "parameters": {}, "function": clipboard_read},
    "clipboard_write": {"description": "写剪贴板", "parameters": {"text": "文本"}, "function": clipboard_write},
    # 进程管理
    "process_list": {"description": "列出进程", "parameters": {}, "function": process_list},
    "process_kill": {"description": "终止进程", "parameters": {"pid": "进程ID"}, "function": process_kill},
    "process_start": {"description": "启动进程", "parameters": {"command": "命令"}, "function": process_start},
    # 窗口管理
    "window_list": {"description": "列出所有窗口", "parameters": {}, "function": window_list},
    "window_activate": {"description": "激活窗口(置顶)", "parameters": {"title": "窗口标题"}, "function": window_activate},
    "window_minimize": {"description": "最小化窗口", "parameters": {"title": "标题"}, "function": window_minimize},
    "window_maximize": {"description": "最大化窗口", "parameters": {"title": "标题"}, "function": window_maximize},
    "window_close": {"description": "关闭窗口", "parameters": {"title": "标题"}, "function": window_close},
    # 应用控制
    "open_app": {"description": "打开应用", "parameters": {"app_name": "名称"}, "function": open_app},
    # 系统控制
    "system_control": {
        "description": "系统操作: screenshot/lock/volume_up/volume_down/mute/shutdown/restart/sleep/hibernate/logoff/cancel_shutdown/empty_recycle_bin/clear_temp/flush_dns",
        "parameters": {"action": "操作名"},
        "function": system_control,
    },
    "system_info": {"description": "获取系统信息", "parameters": {}, "function": system_info},
    # 网络
    "network_request": {"description": "HTTP请求", "parameters": {"url": "URL", "method": "GET/POST(可选)", "data": "POST数据(可选)"}, "function": network_request},
    "network_download": {"description": "下载文件", "parameters": {"url": "URL", "path": "保存路径"}, "function": network_download},
    "network_ping": {"description": "Ping主机", "parameters": {"host": "域名/IP", "count": "次数(可选)"}, "function": network_ping},
    # 环境变量
    "env_get": {"description": "获取环境变量", "parameters": {"name": "变量名"}, "function": env_get},
    "env_set": {"description": "设置环境变量(永久)", "parameters": {"name": "变量名", "value": "值"}, "function": env_set},
    "env_list": {"description": "列出环境变量", "parameters": {}, "function": env_list},
    # 注册表
    "registry_read": {"description": "读取注册表", "parameters": {"key_path": "注册表路径", "value_name": "值名(可选)"}, "function": registry_read},
    "registry_write": {"description": "写入注册表", "parameters": {"key_path": "路径", "value_name": "值名", "value": "值", "type": "类型(可选)"}, "function": registry_write},
    # 服务管理
    "service_list": {"description": "列出系统服务", "parameters": {}, "function": service_list},
    "service_control": {"description": "控制服务", "parameters": {"name": "服务名", "action": "start/stop/restart"}, "function": service_control},
    # ============ 局域网设备 ============
    "device_scan": {"description": "扫描局域网智能设备(SSDP+ARP自动发现识别)", "parameters": {"timeout": "扫描秒数(默认3,可选)"}, "function": device_scan},
    "device_list": {"description": "列出已发现的局域网设备清单", "parameters": {}, "function": device_list},
    "device_control": {"description": "控制局域网设备(开关/亮度/模式等)，需先 device_scan", "parameters": {"device_id": "设备ID(见device_list)", "action": "on/off/toggle/brightness/volume/mode", "value": "数值参数(可选)"}, "function": device_control},
    # ============ 推理闭环：思考笔记本 + 任务终止信号 ============
    "sequential_thinking": {
        "description": "思考笔记本：记录第几步/共几步/本步推理（可选，推荐在 tool_call 前直接写思考）",
        "parameters": {"thought": "本步推理内容", "step": "当前步骤序号(可选)", "total_steps": "预计总步数(可选)"},
        "function": sequential_thinking,
    },
    "task_done": {
        "description": "任务完成信号：真实工具执行完并验证后调用，summary 作为最终交付",
        "parameters": {"summary": "完成总结（做了什么、关键结果）"},
        "function": task_done,
    },
    # tools.py 新增条目
"code_read": {
    "description": "读取文件内容，支持行号范围，用于理解代码结构。",
    "parameters": {"path": "文件路径", "start_line": "起始行号(可选)", "end_line": "结束行号(可选)"},
    "function": _code_read,
},
"code_search": {
    "description": "在项目目录中按关键词搜索文件，返回匹配文件列表。",
    "parameters": {"query": "搜索关键词", "file_pattern": "文件glob模式(可选)"},
    "function": lambda query, file_pattern="*": _code_search(query, file_pattern),
},
"run_code": {
    "description": "执行Python代码片段，返回stdout/stderr。",
    "parameters": {"code": "要执行的Python代码"},
    "function": _run_code,
},
"apply_patch": {
    "description": "将统一diff格式的补丁应用到指定文件。用于精确修改代码。",
    "parameters": {"path": "目标文件路径", "patch": "unified diff内容"},
    "function": _apply_patch,
}
}

# ============ 用户动态工具（对话安装 / 热加载） ============
USER_TOOLS_DIR = Path(__file__).resolve().parent / "user_tools"
USER_TOOLS_STATE = {}   # 文件名 -> {status: ok/error/skipped, tool/error}
_USER_TOOL_NAMES = []   # 当前已注册进 TOOLS 的用户工具名


def _load_user_tool_file(path: Path):
    """加载单个用户工具：约定模块含 SPEC={name,description,parameters} 与 run(**参数)"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("user_tool_" + path.stem, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    meta = getattr(mod, "SPEC", None)
    run = getattr(mod, "run", None)
    if not isinstance(meta, dict) or not meta.get("name") or not callable(run):
        raise ValueError("缺少 SPEC 字典(name/description/parameters) 或 run() 函数")
    name = str(meta["name"]).strip()
    if name in TOOLS and name not in _USER_TOOL_NAMES:
        raise ValueError(f"与内置工具重名: {name}")
    desc = str(meta.get("description", "用户自定义工具"))
    params = meta.get("parameters") or {}

    def _fn(**kwargs):
        try:
            r = run(**kwargs)
            return r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
        except TypeError as e:
            return f"参数错误: {e}"
        except Exception as e:
            return f"工具执行错误: {e}"

    return name, {"description": desc, "parameters": params, "function": _fn}


def load_user_tools() -> dict:
    """扫描 user_tools/ 并热注册（先清掉旧的用户工具），返回加载报告"""
    global _USER_TOOL_NAMES
    for n in _USER_TOOL_NAMES:
        TOOLS.pop(n, None)
    _USER_TOOL_NAMES = []
    USER_TOOLS_STATE.clear()
    USER_TOOLS_DIR.mkdir(exist_ok=True)
    ok = 0
    for f in sorted(USER_TOOLS_DIR.glob("*.py")):
        if f.name.startswith("_"):  # 下划线前缀=停用/模板
            USER_TOOLS_STATE[f.name] = {"status": "skipped"}
            continue
        try:
            name, entry = _load_user_tool_file(f)
            TOOLS[name] = entry
            _USER_TOOL_NAMES.append(name)
            USER_TOOLS_STATE[f.name] = {"status": "ok", "tool": name}
            ok += 1
        except Exception as e:
            USER_TOOLS_STATE[f.name] = {"status": "error", "error": str(e)}
    return {"loaded": ok, "files": dict(USER_TOOLS_STATE)}


def tool_reload() -> str:
    """重新扫描 user_tools/ 并热加载，返回各文件状态"""
    r = load_user_tools()
    lines = [f"已加载 {r['loaded']} 个用户工具"]
    for fn, st in r["files"].items():
        if st["status"] == "ok":
            lines.append(f"  [OK] {fn} -> {st['tool']}")
        elif st["status"] == "error":
            lines.append(f"  [错误] {fn}: {st['error']}")
    return "\n".join(lines)


def tool_unload(name: str) -> str:
    """卸载一个用户工具：文件改名为下划线前缀（停用），并立即从注册表移除"""
    if name not in _USER_TOOL_NAMES:
        return f"未找到已加载的用户工具: {name}"
    src = None
    for fn, st in USER_TOOLS_STATE.items():
        if st.get("tool") == name:
            src = USER_TOOLS_DIR / fn
            break
    if src and src.exists():
        dst = src.with_name("_" + src.name)
        src.rename(dst)
    load_user_tools()
    return f"已卸载用户工具: {name}（文件已停用为 _ 前缀，改回文件名可恢复）"


def list_user_tools() -> list:
    """列出 user_tools 目录全部条目（含停用的），供插件市场展示"""
    out = []
    USER_TOOLS_DIR.mkdir(exist_ok=True)
    for f in sorted(USER_TOOLS_DIR.glob("*.py")):
        if f.name in ("_template.py",):
            continue
        enabled = not f.name.startswith("_")
        st = USER_TOOLS_STATE.get(f.name, {})
        out.append({"file": f.name, "enabled": enabled,
                    "tool": st.get("tool", ""), "status": st.get("status", ""),
                    "error": st.get("error", "")})
    return out


def tool_enable(file: str) -> str:
    """启用一个已停用的用户工具（去掉 _ 前缀并热加载）"""
    f = USER_TOOLS_DIR / os.path.basename(file)
    if not f.exists():
        return f"文件不存在: {file}"
    if not f.name.startswith("_"):
        return "该工具已是启用状态"
    f.rename(f.with_name(f.name.lstrip("_")))
    return tool_reload()


def tool_disable(file: str) -> str:
    """停用一个用户工具（加 _ 前缀改名，从注册表移除；不删文件，可恢复）"""
    f = USER_TOOLS_DIR / os.path.basename(file)
    if not f.exists():
        return f"文件不存在: {file}"
    if f.name.startswith("_"):
        return "该工具已是停用状态"
    f.rename(f.with_name("_" + f.name))
    return tool_reload()


def user_tool_name_to_file() -> dict:
    """返回 {已加载工具名: 文件名} 的反查表，供前端标记「可编辑/可停用」的自定义能力"""
    return {st["tool"]: fn for fn, st in USER_TOOLS_STATE.items()
            if st.get("status") == "ok" and st.get("tool")}


def read_user_tool_file(file: str) -> str:
    """安全读取 user_tools/ 下某个源码文件（防目录穿越），供前端编辑"""
    name = os.path.basename(str(file))
    if not name.endswith(".py"):
        raise ValueError("仅允许 .py 文件")
    fp = USER_TOOLS_DIR / name
    if not fp.exists() or not fp.is_file():
        raise FileNotFoundError(f"文件不存在: {name}")
    return fp.read_text(encoding="utf-8")


def write_user_tool_file(file: str, content: str) -> str:
    """安全写入 user_tools/ 下的源码文件（仅文件名、禁穿越、禁覆盖模板），并热加载"""
    name = os.path.basename(str(file))
    if not name.endswith(".py") or name in (".py",):
        raise ValueError("文件名必须以 .py 结尾")
    if name == "_template.py":
        raise ValueError("不能覆盖模板文件 _template.py")
    if "/" in name or "\\" in name or name.startswith(".."):
        raise ValueError("非法文件名")
    (USER_TOOLS_DIR / name).write_text(content, encoding="utf-8")
    return tool_reload()


TOOLS["tool_reload"] = {
    "description": "重新扫描 user_tools/ 目录并热加载用户工具（新工具落盘确认后调用即可立即生效，无需重启）",
    "parameters": {}, "function": tool_reload}
TOOLS["tool_unload"] = {
    "description": "卸载一个通过对话安装的用户工具（文件停用不删除）",
    "parameters": {"name": "工具名"}, "function": tool_unload}


def get_user_tools_prompt() -> str:
    """列出核心集之外的动态工具（安装指引按需由 self 段注入，不常驻）
    已归入 TOOL_GROUPS 的工具由工具清单按意图列出，这里不重复。"""
    _grouped = {n for g in TOOL_GROUPS.values() for n in g}
    _extra = [n for n in _USER_TOOL_NAMES if n not in CORE_TOOLS and n not in _grouped]
    if not _extra:
        return ""
    lines = ["", "## 用户动态工具（user_tools/）"]
    for n in _extra:
        # MCP 重载会清理已被同名的外部工具，这里必须容错，避免 KeyError: 'kb_search'
        t = TOOLS.get(n)
        if not isinstance(t, dict):
            continue
        ps = ", ".join((t.get("parameters") or {}).keys())
        _desc = str(t["description"]).replace("\n", " ")[:28]
        lines.append(f"- {n}({ps}): {_desc}")
    return "\n".join(lines)


# ============ 操作历史与撤销（写前自动快照，上限100条 FIFO） ============

UNDO_DIR = Path(__file__).resolve().parent / "undo_history"
UNDO_MANIFEST = UNDO_DIR / "manifest.jsonl"
UNDO_LIMIT = 100
UNDO_SNAP_MAX = 5 * 1024 * 1024  # 超过 5MB 的文件不做快照
# 会被自动快照的写操作
_UNDO_TOOLS = {"file_write", "apply_patch", "save_output", "file_delete", "file_move"}


def _undo_load() -> list:
    try:
        if not UNDO_MANIFEST.exists():
            return []
        out = []
        for line in UNDO_MANIFEST.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
        return out
    except Exception:
        return []


def _undo_save(entries: list):
    UNDO_DIR.mkdir(parents=True, exist_ok=True)
    UNDO_MANIFEST.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries),
        encoding="utf-8")


def _undo_target_path(name: str, args: dict) -> str:
    """从工具参数里取操作目标路径"""
    if name == "save_output":
        out_dir = Path(__file__).resolve().parent / "agent" / "outputs"
        return str(out_dir / (Path(str(args.get("filename", "output.txt"))).name))
    return str(args.get("path") or args.get("dst") or "")


def _undo_snapshot(tool_name: str, args: dict):
    """写操作执行前自动快照目标文件，追加到操作历史"""
    target = _undo_target_path(tool_name, args)
    if not target:
        return
    p = Path(target)
    try:
        if UNDO_DIR in p.resolve().parents:  # 不快照历史库自身
            return
    except Exception:
        pass
    entries = _undo_load()
    idx = (entries[-1]["i"] + 1) if entries else 1
    existed = p.is_file()
    snap_name = ""
    note = ""
    if p.is_dir():
        note = "目录不做快照，无法撤销"
    elif existed:
        if p.stat().st_size > UNDO_SNAP_MAX:
            note = "文件超过5MB，未做快照"
        else:
            snap_name = f"snap_{idx}.bin"
            try:
                UNDO_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(p), str(UNDO_DIR / snap_name))
            except Exception:
                snap_name = ""
                note = "快照失败"
    entry = {
        "i": idx,
        "ts": time.time(),
        "time": time.strftime("%H:%M:%S"),
        "tool": tool_name,
        "path": str(p),
        "existed": existed,
        "snap": snap_name,
        "note": note,
    }
    if tool_name == "file_move":
        entry["move_dst"] = str(args.get("dst") or "")
    entries.append(entry)
    while len(entries) > UNDO_LIMIT:  # FIFO：淘汰最老记录及其快照
        old = entries.pop(0)
        if old.get("snap"):
            try:
                (UNDO_DIR / old["snap"]).unlink()
            except Exception:
                pass
    _undo_save(entries)


def _undo_restore(entry: dict) -> str:
    """按一条历史记录还原文件状态，返回结果描述"""
    p = Path(entry["path"])
    snap = entry.get("snap") or ""
    if entry.get("existed") and snap:
        sp = UNDO_DIR / snap
        if not sp.exists():
            return f"#{entry['i']} 快照文件缺失，无法还原 {p.name}"
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(sp), str(p))
        msg = f"#{entry['i']} 已还原 {p.name}（{entry['tool']} 前的内容）"
    elif not entry.get("existed"):
        if p.exists():
            if p.is_file():
                p.unlink()
            else:
                return f"#{entry['i']} {p.name} 是目录，跳过删除"
        msg = f"#{entry['i']} 已删除 {p.name}（该文件是 {entry['tool']} 新建的）"
    else:
        return f"#{entry['i']} {p.name} 无快照（{entry.get('note') or '不可还原'}）"
    if entry.get("move_dst"):  # 撤销移动：把目标位置的文件一并清掉
        dp = Path(entry["move_dst"])
        try:
            if dp.is_file():
                dp.unlink()
        except Exception:
            pass
    return msg


def undo_list() -> str:
    """查看操作历史（最多保留最近100步）"""
    entries = _undo_load()
    if not entries:
        return "暂无操作历史"
    lines = [f"操作历史（共 {len(entries)} 步，最新在最下）："]
    for e in entries:
        restorable = "可撤销" if (e.get("snap") or not e.get("existed")) else "不可撤销"
        lines.append(f"  #{e['i']} [{e['time']}] {e['tool']} -> {e['path']}（{restorable}）")
    lines.append("撤销最新一步: undo_step()；回退到指定节点: undo_to(index)")
    return "\n".join(lines)


def undo_step(path: str = "") -> str:
    """撤销最新一步写操作（指定 path 时撤销该文件的最新一步）"""
    entries = _undo_load()
    if not entries:
        return "暂无可撤销的操作"
    pos = -1
    if path:
        key = str(path).replace("/", os.sep).lower()
        for j in range(len(entries) - 1, -1, -1):
            ep = entries[j]["path"].replace("/", os.sep).lower()
            if ep == key or ep.endswith(os.sep + os.path.basename(key)):
                pos = j
                break
        if pos < 0:
            return f"历史中没有与 {path} 相关的操作"
    else:
        pos = len(entries) - 1
    entry = entries.pop(pos)
    msg = _undo_restore(entry)
    if entry.get("snap"):
        try:
            (UNDO_DIR / entry["snap"]).unlink()
        except Exception:
            pass
    _undo_save(entries)
    return msg + f"\n（剩余 {len(entries)} 步历史）"


def undo_to(index) -> str:
    """批量回退到指定节点：撤销所有比 index 新的操作"""
    try:
        index = int(index)
    except Exception:
        return "参数错误: index 必须是数字（见 undo_list 的 #编号）"
    entries = _undo_load()
    if not entries:
        return "暂无可撤销的操作"
    todo = [e for e in entries if e["i"] > index]
    if not todo:
        return f"没有比 #{index} 更新的操作"
    results = []
    for e in reversed(todo):  # 从最新往旧还原
        results.append(_undo_restore(e))
        if e.get("snap"):
            try:
                (UNDO_DIR / e["snap"]).unlink()
            except Exception:
                pass
    entries = [e for e in entries if e["i"] <= index]
    _undo_save(entries)
    return "\n".join(results) + f"\n已回退到 #{index}（剩余 {len(entries)} 步历史）"


def undo_clear() -> str:
    """清空全部操作历史与快照"""
    entries = _undo_load()
    for e in entries:
        if e.get("snap"):
            try:
                (UNDO_DIR / e["snap"]).unlink()
            except Exception:
                pass
    _undo_save([])
    return f"已清空 {len(entries)} 条操作历史"


TOOLS["undo_list"] = {
    "description": "查看文件操作历史（写操作自动快照，最多100步，含时间/工具/路径）",
    "parameters": {}, "function": undo_list}
TOOLS["undo_step"] = {
    "description": "撤销最新一步文件写操作（可指定 path 只撤销该文件的最新一步）",
    "parameters": {"path": "文件路径(可选)"}, "function": undo_step}
TOOLS["undo_to"] = {
    "description": "批量回退到指定历史节点（撤销所有比 index 新的操作）",
    "parameters": {"index": "目标节点编号"}, "function": undo_to}
TOOLS["undo_clear"] = {
    "description": "清空全部操作历史与快照",
    "parameters": {}, "function": undo_clear}


# ============ 文档原生编写（Word / PPT / Markdown） ============

def doc_write(path: str, kind: str, blocks: str, title: str = "") -> str:
    """按结构化 blocks 生成 Word(.docx)/PPT(.pptx)/Markdown(.md) 文档。

    blocks 为 JSON 数组字符串，元素形如：
      {"t":"h1","text":"标题"} / {"t":"h2","text":"小节"}
      {"t":"p","text":"正文段落"}
      {"t":"bullets","items":["要点1","要点2"]}
      {"t":"table","rows":[["表头1","表头2"],["a","b"]]}
      {"t":"img","path":"图片路径","caption":"说明(可选)"}
    kind: word / ppt / markdown
    """
    try:
        blocks_arr = json.loads(blocks) if isinstance(blocks, str) else blocks
        if not isinstance(blocks_arr, list):
            return "参数错误: blocks 必须是 JSON 数组"
    except Exception as e:
        return f"参数错误: blocks JSON 解析失败: {e}"
    kind = (kind or "").lower().strip()
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    if kind in ("gongwen", "gw", "公文"):
        try:
            return _doc_write_gongwen(p, blocks_arr, title)
        except ImportError:
            return "缺少依赖: pip install python-docx"

    if kind in ("markdown", "md"):
        lines = []
        for b in blocks_arr:
            t = b.get("t")
            if t == "h1":
                lines.append(f"\n# {b.get('text','')}\n")
            elif t == "h2":
                lines.append(f"\n## {b.get('text','')}\n")
            elif t == "h3":
                lines.append(f"\n### {b.get('text','')}\n")
            elif t == "p":
                lines.append(str(b.get("text", "")) + "\n")
            elif t == "bullets":
                lines.extend(f"- {it}" for it in b.get("items", []))
                lines.append("")
            elif t == "table":
                rows = b.get("rows", [])
                if rows:
                    lines.append("| " + " | ".join(str(c) for c in rows[0]) + " |")
                    lines.append("|" + "---|" * len(rows[0]))
                    for r in rows[1:]:
                        lines.append("| " + " | ".join(str(c) for c in r) + " |")
                    lines.append("")
            elif t == "img":
                cap = b.get("caption", "")
                lines.append(f"![{cap}]({b.get('path','')})\n")
            elif t == "quote":
                lines.append(f"> {b.get('text','')}\n")
            elif t == "code":
                lines.append(f"```{b.get('lang','')}\n{b.get('text','')}\n```\n")
        if p.suffix.lower() != ".md":
            p = p.with_suffix(".md")
        p.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return f"Markdown 文档已生成: {p}（{len(blocks_arr)} 个内容块）"

    if kind == "word":
        try:
            import docx
            from docx.shared import Pt
        except ImportError:
            return "缺少依赖: pip install python-docx"
        doc = docx.Document()
        if title:
            doc.add_heading(title, 0)
        for b in blocks_arr:
            t = b.get("t")
            if t == "h1":
                doc.add_heading(str(b.get("text", "")), level=1)
            elif t == "h2":
                doc.add_heading(str(b.get("text", "")), level=2)
            elif t == "h3":
                doc.add_heading(str(b.get("text", "")), level=3)
            elif t == "p":
                doc.add_paragraph(str(b.get("text", "")))
            elif t == "bullets":
                for it in b.get("items", []):
                    doc.add_paragraph(str(it), style="List Bullet")
            elif t == "table":
                rows = b.get("rows", [])
                if rows:
                    tb = doc.add_table(rows=len(rows), cols=len(rows[0]))
                    tb.style = "Table Grid"
                    for ri, row in enumerate(rows):
                        for ci, cell in enumerate(row):
                            tb.rows[ri].cells[ci].text = str(cell)
            elif t == "img":
                ip = b.get("path", "")
                if ip and os.path.isfile(ip):
                    doc.add_picture(ip)
                    if b.get("caption"):
                        doc.add_paragraph(str(b["caption"]))
            elif t == "quote":
                doc.add_paragraph(str(b.get("text", "")), style="Intense Quote")
            elif t == "code":
                para = doc.add_paragraph()
                run = para.add_run(str(b.get("text", "")))
                run.font.name = "Consolas"
                run.font.size = Pt(9)
        if p.suffix.lower() != ".docx":
            p = p.with_suffix(".docx")
        doc.save(str(p))
        return f"Word 文档已生成: {p}（{len(blocks_arr)} 个内容块，可直接用 Office/WPS 打开）"

    if kind == "ppt":
        try:
            from pptx import Presentation
            from pptx.util import Inches, Pt as PPt
        except ImportError:
            return "缺少依赖: pip install python-pptx"
        prs = Presentation()
        blank = prs.slide_layouts[6]
        title_layout = prs.slide_layouts[0]
        content_layout = prs.slide_layouts[1]

        def _add_slide(head, body_items):
            slide = prs.slides.add_slide(content_layout)
            slide.shapes.title.text = head
            tf = slide.placeholders[1].text_frame
            tf.clear()
            for j, it in enumerate(body_items):
                para = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
                para.text = str(it)
                para.font.size = PPt(18)
            return slide

        if title:
            s0 = prs.slides.add_slide(title_layout)
            s0.shapes.title.text = title
        cur_head, cur_body = None, []
        def _flush():
            nonlocal cur_head, cur_body
            if cur_head is not None:
                _add_slide(cur_head, cur_body or [""])
            cur_head, cur_body = None, []
        for b in blocks_arr:
            t = b.get("t")
            if t in ("h1", "h2"):  # 每个标题开一页幻灯片
                _flush()
                cur_head = str(b.get("text", ""))
            elif t == "p":
                cur_body.append(str(b.get("text", "")))
            elif t == "bullets":
                cur_body.extend(str(it) for it in b.get("items", []))
            elif t == "table":
                rows = b.get("rows", [])
                if rows:
                    if cur_head is None:
                        cur_head = "数据表"
                    slide = prs.slides.add_slide(blank)
                    tb = slide.shapes.add_table(
                        len(rows), len(rows[0]),
                        Inches(0.8), Inches(1.2), Inches(8.4), Inches(0.4 * len(rows))).table
                    for ri, row in enumerate(rows):
                        for ci, cell in enumerate(row):
                            tb.cell(ri, ci).text = str(cell)
            elif t == "img":
                ip = b.get("path", "")
                if ip and os.path.isfile(ip):
                    slide = prs.slides.add_slide(blank)
                    slide.shapes.add_picture(ip, Inches(1), Inches(1), height=Inches(4.5))
        _flush()
        if p.suffix.lower() != ".pptx":
            p = p.with_suffix(".pptx")
        prs.save(str(p))
        return f"PPT 已生成: {p}（{len(prs.slides._sldIdLst)} 页，可直接用 Office/WPS 打开）"

    return "参数错误: kind 必须是 word / ppt / markdown"


# ============ 公文格式（GB/T 9704-2012）生成：发改委可研报告等正式文件 ============

def _gw_font(run, cn, size, bold=False):
    """中文字体必须同时写 eastAsia，否则 Word/WPS 会回退成默认宋体"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt
    run.font.name = cn
    run.font.size = Pt(size)
    run.bold = bold
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    rFonts.set(qn("w:ascii"), "Times New Roman")
    rFonts.set(qn("w:hAnsi"), "Times New Roman")
    rFonts.set(qn("w:eastAsia"), cn)


def _gw_para(doc, text, cn="仿宋_GB2312", size=16, bold=False, align="both",
             indent=2, line=28, before=0, after=0):
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt
    _ALIGN = {"both": WD_ALIGN_PARAGRAPH.JUSTIFY, "left": WD_ALIGN_PARAGRAPH.LEFT,
              "center": WD_ALIGN_PARAGRAPH.CENTER, "right": WD_ALIGN_PARAGRAPH.RIGHT}
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.line_spacing = Pt(line)
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    pf.first_line_indent = Pt(size * indent) if indent else Pt(0)
    pf.alignment = _ALIGN.get(align, WD_ALIGN_PARAGRAPH.JUSTIFY)
    if text:
        _gw_font(p.add_run(str(text)), cn, size, bold)
    return p


def _doc_write_gongwen(p, blocks_arr, title=""):
    """按党政机关公文格式生成 .docx：
    正文三号仿宋_GB2312、行距固定 28 磅、首行缩进 2 字符；
    标题二号小标宋体居中；一级标题黑体、二级楷体_GB2312；页边距 37/35/28/26mm。
    额外支持块：title / to / attachment / sign / pagebreak / h4。
    """
    import docx
    from docx.shared import Cm, Pt
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    doc = docx.Document()

    # A4 + 公文页边距
    sec = doc.sections[0]
    sec.page_width = Cm(21.0)
    sec.page_height = Cm(29.7)
    sec.top_margin = Cm(3.7)
    sec.bottom_margin = Cm(3.5)
    sec.left_margin = Cm(2.8)
    sec.right_margin = Cm(2.6)

    # 默认正文样式（表格/空段落也继承）
    normal = doc.styles["Normal"]
    normal.font.name = "仿宋_GB2312"
    normal.font.size = Pt(16)
    rpr = normal.element.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rpr.append(rf)
    rf.set(qn("w:eastAsia"), "仿宋_GB2312")

    if title:
        _gw_para(doc, title, cn="方正小标宋简体", size=22, align="center",
                 indent=0, line=36, after=18)

    for b in blocks_arr:
        t = str(b.get("t") or "").lower()
        if t == "title":
            _gw_para(doc, b.get("text", ""), cn="方正小标宋简体", size=22,
                     align="center", indent=0, line=36, after=18)
        elif t == "to":            # 主送机关（顶格）
            _gw_para(doc, b.get("text", ""), indent=0, align="left")
        elif t == "h1":            # 一、二、三（黑体）
            _gw_para(doc, b.get("text", ""), cn="黑体")
        elif t == "h2":            # （一）（二）（楷体）
            _gw_para(doc, b.get("text", ""), cn="楷体_GB2312")
        elif t == "h3":            # 1. 2.（仿宋加粗）
            _gw_para(doc, b.get("text", ""), bold=True)
        elif t == "h4":            # （1）（2）
            _gw_para(doc, b.get("text", ""))
        elif t == "p":
            _gw_para(doc, b.get("text", ""))
        elif t == "bullets":
            for it in (b.get("items") or []):
                _gw_para(doc, "· " + str(it))
        elif t == "table":
            rows = b.get("rows") or []
            if rows:
                tb = doc.add_table(rows=len(rows), cols=len(rows[0]))
                tb.style = "Table Grid"
                tb.alignment = 1
                for ri, row in enumerate(rows):
                    for ci, cell in enumerate(row):
                        c = tb.rows[ri].cells[ci]
                        c.text = ""
                        para = c.paragraphs[0]
                        para.alignment = 1
                        _gw_font(para.add_run(str(cell)), "仿宋_GB2312", 12, ri == 0)
        elif t == "img":
            ip = b.get("path", "")
            if ip and os.path.isfile(ip):
                doc.add_picture(ip, width=Cm(14.5))
                doc.paragraphs[-1].alignment = 1
                if b.get("caption"):
                    _gw_para(doc, b["caption"], cn="楷体_GB2312", size=14,
                             align="center", indent=0, line=24)
        elif t == "quote":
            _gw_para(doc, b.get("text", ""), cn="楷体_GB2312")
        elif t == "attachment":    # 附件说明（顶格）
            _gw_para(doc, b.get("text", ""), indent=0, align="left")
        elif t == "sign":          # 落款：单位 + 日期右对齐
            org = b.get("org") or b.get("text") or ""
            date = b.get("date") or ""
            _gw_para(doc, "", indent=0)
            if org:
                _gw_para(doc, org, align="right", indent=0)
            if date:
                _gw_para(doc, date, align="right", indent=0)
        elif t == "pagebreak":
            doc.add_page_break()
        elif t == "code":
            _gw_para(doc, b.get("text", ""), cn="宋体", size=12, indent=0, line=20)

    # 页脚页码：四号宋体居中
    footer = sec.footer.paragraphs[0]
    footer.alignment = 1
    frun = footer.add_run()
    _gw_font(frun, "宋体", 14)
    for tag, attr, text in (("w:fldChar", "begin", None),
                            ("w:instrText", None, " PAGE "),
                            ("w:fldChar", "end", None)):
        el = OxmlElement(tag)
        if text is not None:
            el.set(qn("xml:space"), "preserve")
            el.text = text
        else:
            el.set(qn("w:fldCharType"), attr)
        frun._r.append(el)

    if p.suffix.lower() != ".docx":
        p = p.with_suffix(".docx")
    doc.save(str(p))
    return ("公文格式 Word 已生成: %s（A4，正文三号仿宋_GB2312，行距 28 磅，"
            "页边距 37/35/28/26mm，%d 个内容块）" % (p, len(blocks_arr)))


TOOLS["doc_write"] = {
    "description": "原生编写 Word/PPT/Markdown 文档：传结构化 blocks(JSON数组) 一键排版生成 .docx/.pptx/.md。kind=gongwen 时按党政机关公文格式（GB/T 9704-2012）排版，用于发改委可行性研究报告、立项请示、批复、通知等正式公文",
    "parameters": {"path": "输出文件路径", "kind": "word/ppt/markdown/gongwen(公文格式)", "blocks": "内容块JSON数组，如[{\"t\":\"h1\",\"text\":\"一、项目概述\"},{\"t\":\"p\",\"text\":\"正文\"},{\"t\":\"table\",\"rows\":[[\"表头\",\"表头\"],[\"a\",\"b\"]]}]；公文另有 title/to(主送机关)/attachment(附件)/sign(落款)/pagebreak", "title": "文档总标题(可选，公文为大标题)"},
    "function": doc_write}


# ============ AI 视频生成（ffmpeg 分片渲染 · 断点续渲） ============

_FFMPEG = str(Path(__file__).resolve().parent / "tools" / "ffmpeg.exe")
_VIDEO_BASE = Path(__file__).resolve().parent / "agent" / "outputs" / "video"

# 默认参数模板（video_new 不传参数时用这套，也可一键恢复默认）
VIDEO_DEFAULTS = {
    "resolution": "1280x720",
    "fps": 30,
    "samplerate": 44100,
    "style": "电影感",
    "bitrate": "4M",
}

# 风格预设 → ffmpeg 滤镜链（作用在画面调色上）
VIDEO_STYLES = {
    "电影感": "eq=contrast=1.12:saturation=0.88:brightness=-0.02,vignette=PI/4.5",
    "清新":   "eq=brightness=0.06:saturation=1.25:contrast=1.02",
    "复古":   "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131,noise=alls=6:allf=t",
    "科技":   "eq=contrast=1.18:brightness=-0.03:saturation=1.05,hue=h=-8",
    "原片":   "null",
}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".webm", ".m4v"}
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}


def _video_proj_dir(project: str) -> Path:
    name = re.sub(r"[^\w一-鿿-]", "_", str(project or "untitled"))[:40] or "untitled"
    return _VIDEO_BASE / name


def _video_load(project: str):
    d = _video_proj_dir(project)
    pf = d / "project.json"
    if not pf.exists():
        return None, d, f"工程不存在: {project}（先用 video_new 创建）"
    try:
        return json.loads(pf.read_text(encoding="utf-8")), d, ""
    except Exception as e:
        return None, d, f"工程文件损坏: {e}"


def _video_save(d: Path, proj: dict):
    d.mkdir(parents=True, exist_ok=True)
    (d / "project.json").write_text(
        json.dumps(proj, ensure_ascii=False, indent=2), encoding="utf-8")


def _video_progress(d: Path, state: str, done: int, total: int, extra: str = ""):
    (d / "progress.json").write_text(json.dumps({
        "state": state, "done": done, "total": total,
        "percent": round(done * 100.0 / total, 1) if total else 0,
        "extra": extra,
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False), encoding="utf-8")


def video_new(project: str, title: str = "", resolution: str = "",
              fps: int = 0, duration: int = 0, samplerate: int = 0,
              style: str = "") -> str:
    """新建视频工程（不写参数则用默认模板；duration 为目标总时长秒数，仅作规划参考）"""
    d = _video_proj_dir(project)
    if (d / "project.json").exists():
        return f"工程已存在: {project}（重复创建会覆盖，请先换名或删除目录 {d}）"
    cfg = dict(VIDEO_DEFAULTS)
    if resolution:
        if not re.match(r"^\d{2,5}x\d{2,5}$", str(resolution)):
            return "参数错误: resolution 形如 1280x720 / 1920x1080"
        cfg["resolution"] = str(resolution)
    if fps:
        cfg["fps"] = max(1, min(120, int(fps)))
    if samplerate:
        cfg["samplerate"] = int(samplerate)
    if style:
        if style not in VIDEO_STYLES:
            return f"未知风格: {style}。可选: {'、'.join(VIDEO_STYLES.keys())}"
        cfg["style"] = style
    proj = {
        "name": project, "title": title or project,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "params": cfg,
        "target_seconds": int(duration or 0),
        "segments": [],
    }
    _video_save(d, proj)
    _video_progress(d, "editing", 0, 0)
    return (f"视频工程已创建: {d}\n参数: {cfg['resolution']} · {cfg['fps']}fps · "
            f"采样率 {cfg['samplerate']} · 风格「{cfg['style']}」\n"
            f"下一步: video_add_segment 添加素材片段，最后 video_render 渲染")


def video_defaults() -> str:
    """查看默认参数模板与全部风格预设（一键恢复默认: video_new 不传参即默认）"""
    lines = ["默认参数模板:"]
    for k, v in VIDEO_DEFAULTS.items():
        lines.append(f"  {k} = {v}")
    lines.append("可选风格: " + "、".join(VIDEO_STYLES.keys()))
    lines.append("支持分辨率: 640x360 / 854x480 / 1280x720 / 1920x1080 / 2560x1440 / 3840x2160（自定义 WxH 亦可）")
    return "\n".join(lines)


def video_set(project: str, resolution: str = "", fps: int = 0,
              samplerate: int = 0, style: str = "", title: str = "") -> str:
    """修改工程参数 / 一键切换风格（渲染前随时可改）"""
    proj, d, err = _video_load(project)
    if err:
        return err
    changed = []
    if resolution:
        if not re.match(r"^\d{2,5}x\d{2,5}$", str(resolution)):
            return "参数错误: resolution 形如 1280x720"
        proj["params"]["resolution"] = str(resolution)
        changed.append(f"分辨率→{resolution}")
    if fps:
        proj["params"]["fps"] = max(1, min(120, int(fps)))
        changed.append(f"帧率→{fps}")
    if samplerate:
        proj["params"]["samplerate"] = int(samplerate)
        changed.append(f"采样率→{samplerate}")
    if style:
        if style not in VIDEO_STYLES:
            return f"未知风格: {style}。可选: {'、'.join(VIDEO_STYLES.keys())}"
        proj["params"]["style"] = style
        changed.append(f"风格→{style}")
    if title:
        proj["title"] = title
        changed.append(f"标题→{title}")
    if not changed:
        return "未提供任何要修改的参数"
    _video_save(d, proj)
    return "已更新: " + "，".join(changed)


def video_add_segment(project: str, material_path: str = "", text: str = "",
                      seconds: float = 5) -> str:
    """向工程添加一个片段：本地素材（图片/视频）+ 字幕文案 + 时长。长视频=多个片段叠加"""
    proj, d, err = _video_load(project)
    if err:
        return err
    if not material_path:
        return "参数错误: material_path 必填（本地图片或视频素材路径）"
    mp, note = _resolve_file_path(material_path)
    if not mp.exists():
        return note or f"素材不存在: {material_path}"
    ext = mp.suffix.lower()
    if ext not in _VIDEO_EXTS | _IMG_EXTS:
        return f"不支持的素材格式: {ext}（图片: {sorted(_IMG_EXTS)}；视频: {sorted(_VIDEO_EXTS)}）"
    try:
        seconds = float(seconds)
    except Exception:
        seconds = 5.0
    seconds = max(0.5, min(3600.0, seconds))
    seg = {
        "idx": len(proj["segments"]),
        "material": str(mp),
        "kind": "video" if ext in _VIDEO_EXTS else "image",
        "text": str(text or ""),
        "seconds": seconds,
    }
    proj["segments"].append(seg)
    total = sum(s["seconds"] for s in proj["segments"])
    _video_save(d, proj)
    _video_progress(d, "editing", 0, len(proj["segments"]))
    return (note + f"已添加片段 #{seg['idx']}: {mp.name}（{seconds}s）\n"
            f"当前共 {len(proj['segments'])} 个片段，总时长约 {total:.0f}s"
            f"（{total/60:.1f} 分钟）")


def _video_seg_filter(proj: dict, seg: dict) -> str:
    """构造单个片段的 ffmpeg 滤镜链"""
    w, h = proj["params"]["resolution"].split("x")
    fps = proj["params"]["fps"]
    style_vf = VIDEO_STYLES.get(proj["params"]["style"], "null")
    frames = max(1, int(seg["seconds"] * fps))
    if seg["kind"] == "image":
        # 图片 → 缓慢推镜（zoompan），先放大再采样保证画质
        vf = (f"scale={int(w)*2}:{int(h)*2}:force_original_aspect_ratio=increase,"
              f"crop={int(w)*2}:{int(h)*2},"
              f"zoompan=z='min(1+0.0012*on,1.18)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
              f"d={frames}:s={w}x{h}:fps={fps}")
    else:
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={fps}")
    if style_vf and style_vf != "null":
        vf += "," + style_vf
    txt = (seg.get("text") or "").strip()
    if txt:
        safe = re.sub(r"['\\:%\[\];]", "", txt)[:80]
        font = "C\\:/Windows/Fonts/msyh.ttc"
        vf += (f",drawtext=fontfile='{font}':text='{safe}':fontsize={max(24, int(h)//18)}:"
               f"fontcolor=white:borderw=2:bordercolor=black@0.65:"
               f"x=(w-text_w)/2:y=h-{max(60, int(h)//6)}")
    return vf


def video_render(project: str) -> str:
    """分片渲染工程：每个片段独立渲染成临时 mp4（已渲染的自动跳过→断点续渲，长视频不中断），最后合并输出成品"""
    proj, d, err = _video_load(project)
    if err:
        return err
    if not os.path.isfile(_FFMPEG):
        return f"ffmpeg 未找到: {_FFMPEG}"
    segs = proj["segments"]
    if not segs:
        return "工程还没有片段，先用 video_add_segment 添加素材"
    p = proj["params"]
    sr = p["samplerate"]
    fps = p["fps"]
    seg_dir = d / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    done = 0
    total = len(segs)
    _video_progress(d, "rendering", 0, total, "开始分片渲染")
    seg_files = []
    for seg in segs:
        seg_mp4 = seg_dir / f"seg_{seg['idx']:03d}.mp4"
        seg_files.append(seg_mp4)
        marker = seg_dir / f"seg_{seg['idx']:03d}.ok"
        # 断点续渲：已完成的片段跳过（参数或素材变更后需删除 segments 目录重渲）
        if seg_mp4.exists() and marker.exists():
            done += 1
            _video_progress(d, "rendering", done, total, f"片段 {seg['idx']} 已存在，跳过")
            continue
        vf = _video_seg_filter(proj, seg)
        if seg["kind"] == "image":
            cmd = [_FFMPEG, "-y", "-loop", "1", "-i", seg["material"],
                   "-f", "lavfi", "-i", f"anullsrc=r={sr}:cl=stereo"]
        else:
            cmd = [_FFMPEG, "-y", "-i", seg["material"],
                   "-f", "lavfi", "-i", f"anullsrc=r={sr}:cl=stereo"]
        cmd += ["-t", str(seg["seconds"]), "-vf", vf,
                "-c:v", "libx264", "-preset", "fast", "-b:v", p.get("bitrate", "4M"),
                "-pix_fmt", "yuv420p", "-r", str(fps),
                "-c:a", "aac", "-ar", str(sr), "-shortest",
                "-map", "0:v", "-map", "1:a", str(seg_mp4)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=max(120, int(seg["seconds"] * 8)),
                               encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            _video_progress(d, "error", done, total, f"片段 {seg['idx']} 渲染超时")
            return f"片段 #{seg['idx']} 渲染超时（可重新调用 video_render 断点续渲）"
        if r.returncode != 0 or not seg_mp4.exists():
            _video_progress(d, "error", done, total, f"片段 {seg['idx']} 失败")
            return (f"片段 #{seg['idx']} 渲染失败:\n{r.stderr[-600:]}\n"
                    f"（修复后重新调用 video_render 即可续渲，已完成片段不会重渲）")
        marker.write_text("ok", encoding="utf-8")
        done += 1
        _video_progress(d, "rendering", done, total, f"片段 {seg['idx']} 完成")
    # 合并所有分片（同一编码参数，流复制无损秒拼）
    list_file = seg_dir / "concat.txt"
    list_file.write_text(
        "\n".join(f"file '{f.as_posix()}'" for f in seg_files), encoding="utf-8")
    out_dir = Path(__file__).resolve().parent / "agent" / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r"[^\w一-鿿-]", "_", proj.get("title") or proj["name"])[:40]
    out_mp4 = out_dir / f"{safe_title}.mp4"
    r = subprocess.run([_FFMPEG, "-y", "-f", "concat", "-safe", "0",
                        "-i", str(list_file), "-c", "copy", str(out_mp4)],
                       capture_output=True, text=True, timeout=600,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not out_mp4.exists():
        _video_progress(d, "error", done, total, "合并失败")
        return f"分片全部完成，但合并失败:\n{r.stderr[-600:]}"
    total_s = sum(s["seconds"] for s in segs)
    size_mb = round(out_mp4.stat().st_size / 1024 / 1024, 1)
    _video_progress(d, "done", total, total, str(out_mp4))
    return (f"✅ 渲染完成: {out_mp4}\n共 {total} 个片段 · {total_s:.0f}s"
            f"（{total_s/60:.1f} 分钟）· {size_mb}MB · "
            f"{p['resolution']}@{p['fps']}fps · 风格「{p['style']}」")


def video_status(project: str) -> str:
    """查看工程状态：参数、片段清单、渲染进度"""
    proj, d, err = _video_load(project)
    if err:
        return err
    lines = [f"工程: {proj['name']}（{proj.get('title','')}）",
             f"参数: {proj['params']['resolution']} · {proj['params']['fps']}fps · "
             f"采样率 {proj['params']['samplerate']} · 风格「{proj['params']['style']}」",
             f"片段（{len(proj['segments'])} 个）:"]
    for s in proj["segments"]:
        lines.append(f"  #{s['idx']} [{s['kind']}] {Path(s['material']).name} · "
                     f"{s['seconds']}s · {s.get('text','')[:20]}")
    pf = d / "progress.json"
    if pf.exists():
        try:
            pg = json.loads(pf.read_text(encoding="utf-8"))
            lines.append(f"进度: {pg['state']} {pg['done']}/{pg['total']}"
                         f"（{pg['percent']}%）{pg.get('extra','')}")
        except Exception:
            pass
    return "\n".join(lines)


TOOLS["video_new"] = {
    "description": "新建 AI 视频工程（分辨率/帧率/采样率/风格全参数可配，不传参用默认模板）。用户说'生成视频/做个视频'时第一步调用",
    "parameters": {"project": "工程名(英文/数字)", "title": "视频标题(可选)", "resolution": "如1280x720/1920x1080(可选)", "fps": "帧率(可选)", "duration": "目标总时长秒数,长视频如600(可选)", "samplerate": "音频采样率(可选)", "style": "风格(可选): 电影感/清新/复古/科技/原片"},
    "function": video_new}
TOOLS["video_add_segment"] = {
    "description": "向视频工程添加片段：本地素材(图片或视频)+字幕文案+时长。长视频通过多次调用叠加片段实现",
    "parameters": {"project": "工程名", "material_path": "本地素材路径", "text": "该片段字幕文案(可选)", "seconds": "片段时长秒数(默认5)"},
    "function": video_add_segment}
TOOLS["video_render"] = {
    "description": "分片渲染视频工程并合并输出 mp4。已渲染片段自动跳过(断点续渲)，10分钟以上长视频也不会因中断前功尽弃",
    "parameters": {"project": "工程名"},
    "function": video_render}
TOOLS["video_status"] = {
    "description": "查看视频工程参数/片段清单/渲染进度",
    "parameters": {"project": "工程名"},
    "function": video_status}
TOOLS["video_set"] = {
    "description": "修改视频工程参数或一键切换风格（渲染前生效）",
    "parameters": {"project": "工程名", "resolution": "(可选)", "fps": "(可选)", "samplerate": "(可选)", "style": "电影感/清新/复古/科技/原片(可选)", "title": "(可选)"},
    "function": video_set}
TOOLS["video_defaults"] = {
    "description": "查看视频默认参数模板与全部风格预设",
    "parameters": {},
    "function": video_defaults}


# ============ 工具参数校验层（7B 常乱传参数：多余字段 / 缺必填 / 类型错 / 名字拼错） ============
# 直接从 Python 函数签名推断 schema（必填 = 无默认值；类型 = 注解），无需逐个工具手写声明。
_TYPE_MAP = {"str": str, "int": int, "float": float, "bool": bool}


def validate_tool_args(name: str, args) -> str:
    """校验工具参数。返回错误描述字符串；全部合法返回空串。
    校验项：必填缺失 / 多余字段 / 类型不匹配。未知工具或无签名时返回空串（放行）。"""
    if name not in TOOLS:
        return ""
    if not isinstance(args, dict):
        return f"参数必须是对象(dict)，实际是 {type(args).__name__}"
    func = TOOLS[name].get("function")
    if func is None:
        return ""
    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        return ""
    params = {p: info for p, info in sig.parameters.items()
              if p != "self" and info.kind not in (inspect.Parameter.VAR_KEYWORD,
                                                   inspect.Parameter.VAR_POSITIONAL)}
    # 用户工具统一包装为 _fn(**kwargs)：真实参数清单以 SPEC.parameters 为准（均按可选处理）
    if any(i.kind == inspect.Parameter.VAR_KEYWORD for i in sig.parameters.values()):
        allowed = set((TOOLS[name].get("parameters") or {}).keys())
        extra = [k for k in args if k not in allowed]
        return "；".join(
            f"多余参数「{k}」（该工具只接受：{', '.join(sorted(allowed)) or '无参'}）"
            for k in extra)
    errors = []
    # 1) 必填缺失
    for p, info in params.items():
        if info.default is inspect.Parameter.empty and p not in args:
            errors.append(f"缺少必填参数「{p}」")
    # 2) 多余字段
    for k in args:
        if k not in params:
            errors.append(f"多余参数「{k}」（该工具只接受：{', '.join(params) or '无参'}）")
    # 3) 类型不匹配（按注解）
    for k, v in args.items():
        if k not in params:
            continue
        ann = params[k].annotation
        if ann is inspect.Parameter.empty:
            continue
        expected = _TYPE_MAP.get(str(ann))
        if expected and not isinstance(v, expected):
            errors.append(f"参数「{k}」类型错误：期望 {expected.__name__}，实际是 {type(v).__name__}")
    return "；".join(errors)


def tool_call_example(name: str) -> str:
    """生成该工具的正确调用 JSON 示例（只填必填字段），供校验失败时回灌给模型参考。"""
    if name not in TOOLS:
        return ""
    func = TOOLS[name].get("function")
    if func is None:
        return ""
    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        return ""
    sample = {}
    for p, info in sig.parameters.items():
        if p == "self":
            continue
        if info.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
            continue
        if info.default is not inspect.Parameter.empty:
            continue
        ann = info.annotation
        sample[p] = "实际路径/内容" if ann in (str, inspect.Parameter.empty) else (
            0 if ann == int else 0.0 if ann == float else False)
    return json.dumps({"name": name, "parameters": sample}, ensure_ascii=False)


def key_args_hash(name: str, args) -> str:
    """提取「关键参数」（必填字段）做哈希，用于重复调用检测。
    模型常乱传可选/无关字段导致「看起来不同但实际是同一调用」。"""
    if not isinstance(args, dict) or name not in TOOLS:
        return json.dumps(args, sort_keys=True, ensure_ascii=False)
    func = TOOLS[name].get("function")
    if func is None:
        return json.dumps(args, sort_keys=True, ensure_ascii=False)
    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        return json.dumps(args, sort_keys=True, ensure_ascii=False)
    required = {p for p, info in sig.parameters.items()
                if p != "self" and info.default is inspect.Parameter.empty}
    key = {k: args[k] for k in required if k in args}
    return name + "|" + json.dumps(key, sort_keys=True, ensure_ascii=False)


def _norm_tool_args(name: str, args: dict) -> dict:
    """统一规范化路径类参数：去首尾引号空白、统一分隔符、相对路径自动补全到项目根。
    7B 模型常把路径写成 "wasily/AI"（相对路径）或 D:/xxx 混合分隔符，导致「没找到文件」这类误答。"""
    if not isinstance(args, dict):
        return args
    # 只对"要求目标已存在"的工具做存在性补全；写/新建类只做格式规范化
    _need_exists = name in ("file_read", "file_list", "file_search", "file_delete",
                            "code_read", "code_search", "file_copy", "file_move")
    _skip_key = re.compile(r"pattern|glob|query|command|content|url|text|reason|name|code", re.I)
    _path_key = re.compile(r"path|file|dir|src|dst|directory|cwd", re.I)
    out = {}
    for k, v in args.items():
        if not (isinstance(v, str) and _path_key.search(str(k)) and not _skip_key.search(str(k))):
            out[k] = v
            continue
        s = v.strip().strip('"').strip("'").strip()
        if re.match(r"(file|http|https)://", s, re.I):
            out[k] = s
            continue
        # 隔离运行只相对本次 worktree 解析，禁止向上搜索主仓库同名文件。
        s = s.replace("/", os.sep)
        if get_workspace_isolated() and s and not os.path.isabs(s):
            s = os.path.join(get_workspace_root(), s)
        elif _need_exists and s and not os.path.isabs(s) and not os.path.exists(s):
            _cur = os.path.dirname(os.path.abspath(__file__))
            _rel = s
            for _ in range(4):
                for cand in (os.path.join(_cur, _rel),
                             os.path.join(_cur, _rel.lstrip("\\/"))):
                    if os.path.exists(cand):
                        s = cand
                        break
                if os.path.exists(s):
                    break
                _parent = os.path.dirname(_cur)
                if _parent == _cur:
                    break
                _cur = _parent
        out[k] = s
    return out


# Ask / Plan（非 solo）只读拦截：这些工具一律不执行（含 IDE 桥写入/终端）
READONLY_BLOCKED_TOOLS = {
    "file_write", "save_output", "file_delete", "file_move", "file_rename",
    "shell_exec", "process_start", "process_kill", "tool_approve",
    "ide_edit_file", "ide_run_terminal",
}
# 纯读取意图下从工具列表里隐藏的写类工具（shell_exec 也隐藏，避免重定向/删除等副作用）
PURE_READ_HIDDEN_TOOLS = READONLY_BLOCKED_TOOLS

# 隔离 worktree 内禁止可绕过路径契约的系统级出口；普通项目命令仍走 shell_exec。
ISOLATED_BLOCKED_TOOLS = {
    "shell_admin", "ide_run_terminal", "process_start", "process_kill",
    "run_python", "run_code", "save_output", "tool_reload", "tool_unload",
    "registry_write", "service_control", "env_set", "system_control",
    "undo_step", "undo_to", "undo_clear",
}
ISOLATED_REJECT_MSG = "⚠️ 隔离运行禁止该工具，请在 worktree 内使用文件工具或 shell_exec"


def _isolated_tool_error(name, args):
    if not get_workspace_isolated():
        return ""
    if name in ISOLATED_BLOCKED_TOOLS:
        return ISOLATED_REJECT_MSG + ": " + name
    if name == "shell_exec":
        return _isolated_command_error((args or {}).get("command"))
    if not isinstance(args, dict):
        return ""
    skip = re.compile(r"pattern|glob|query|command|content|url|text|reason|name|code", re.I)
    path_key = re.compile(r"path|file|dir|src|dst|directory|cwd", re.I)
    for key, value in args.items():
        if not isinstance(value, str) or not value.strip():
            continue
        if path_key.search(str(key)) and not skip.search(str(key)):
            try:
                scope_path(value)
            except WorkspaceBoundaryError as exc:
                return "⚠️ " + str(exc)
    return ""


READONLY_REJECT_MSG = ("⚠️ 对话模式下不会自动修改文件/执行命令。"
                       "请切换到 **SOLO 模式（Builder）** 让 AI 自主执行，"
                       "或把文件通过资源管理器右键「添加到对话」后手动描述要改什么。")


# ============ 工具分组与默认集（7B 上下文预算，可按关键词/模式解锁） ============
# 回退开关：config.json 的 tools.mode = "full"，或环境变量 WASILY_TOOLS_FULL=1，
# 立即恢复“列出全部工具”的旧行为。未列出的工具仍可被调用（execute_tool 不做白名单拦截）。
LIGHT_READ_TOOLS = ("file_list", "file_read", "file_search", "code_read", "code_search")

CORE_TOOLS = (
    # 读取类（含新契约 v2）
    "file_read", "code_read", "code_search", "file_search", "file_list",
    "network_request",
    # 写入/编辑
    "file_write", "apply_patch", "ide_edit_file", "ide_open_file",
    "ide_read_file", "ide_diagnostics",
    # 执行
    "shell_exec", "run_python", "ide_run_terminal",
    # 记忆/任务/推理
    "memory_save", "memory_list", "mem_recall", "task_save", "task_update",
    "task_done", "sequential_thinking", "todo_list", "time_now", "system_info",
    # 主动提问（关键决策点暂停，等用户选择/补充）
    "ask_user",
    # 子智能体委派（独立上下文，防爆窗）
    "agent_delegate", "agent_list",
)

TOOL_GROUPS = {
    # 文档/图片/网页读取按需挂载（常驻不列，省 ~190 tokens；file_read 仍会自动路由）
    "readers": ("read_pdf", "read_office", "read_image", "fetch_web"),
    "webopen": ("resolve_homepage", "open_homepage"),
    "desktop": ("mouse_move", "mouse_click", "mouse_double_click", "mouse_right_click",
                "mouse_drag", "mouse_scroll", "mouse_position", "key_type", "key_press",
                "key_hold", "screenshot", "clipboard_read", "clipboard_write",
                "window_list", "window_activate", "window_minimize", "window_maximize",
                "window_close", "open_app"),
    "system": ("system_control", "env_get", "env_set", "env_list", "registry_read",
               "registry_write", "service_list", "service_control", "process_list",
               "process_kill", "process_start", "sys_clean", "wallpaper"),
    "device": ("device_scan", "device_list", "device_control"),
    "media": ("video_new", "video_add_segment", "video_render", "video_status",
              "video_set", "video_defaults", "doc_write"),
    "ops": ("save_output", "file_copy", "file_move", "file_delete", "undo_list",
            "undo_step", "undo_to", "undo_clear", "shell_admin", "run_code",
            "tool_reload", "tool_unload"),
    "net": ("network_download", "network_ping", "web_snap", "currency", "weather",
            "qrcode_maker"),
    "dev": ("npm_runner", "php_server", "python_server", "lora_manage", "train_start",
            "train_status", "train_stop"),
}

GROUP_KEYWORDS = {
    "readers": ("pdf", "word", "docx", "excel", "xlsx", "ppt", "pptx", "office",
                "图片", "照片", "图像", "网页", "网址", "链接", "url", "http",
                "image", "photo", "picture", "web", "fetch", "抓取"),
    "webopen": ("打开网页", "打开主页", "打开首页", "打开浏览器", "打开网址", "开个网页",
                "浏览器", "主页", "首页", "网址", "打开百度", "打开谷歌",
                "https://", "http://", "www."),
    "desktop": ("截图", "屏幕", "鼠标", "点击", "键盘", "剪贴板", "窗口", "打开应用",
                "screenshot", "mouse", "click", "keyboard", "clipboard", "window"),
    "system": ("环境变量", "注册表", "服务", "进程", "清理", "壁纸", "关机", "重启",
               "registry", "service", "process", "env", "wallpaper", "system"),
    "device": ("设备", "摄像头", "串口", "蓝牙", "device", "camera", "serial", "ble"),
    "media": ("视频", "幻灯片", "ppt", "word", "docx", "生成文档", "video", "slide", "doc"),
    "ops": ("撤销", "回滚", "复制文件", "移动文件", "删除文件", "管理员", "重载工具",
            "undo", "rollback", "copy", "move", "delete", "reload"),
    "net": ("下载", "ping", "网页截图", "天气", "汇率", "二维码", "download", "weather",
            "currency", "qrcode"),
    "dev": ("npm", "php", "服务端", "lora", "训练", "toolchain", "train"),
}

# MCP 只在明确相关时展示；通用浏览器请求优先用内置 browser，Playwright 由名称显式解锁。
# 其它第三方 server 仍可通过 server 名、完整工具名或 tools.mode=full 使用。
_MCP_SERVER_KEYWORDS = {
    "echo": ("echo", "回显", "mcp__echo__"),
    "fs": ("filesystem", "mcp__fs__", "mcp文件"),
    "pw": ("playwright", "mcp__pw__"),
    "remote": ("remote", "远程mcp", "mcp__remote__"),
    "kb": ("知识库", "文档库", "资料库", "kb_", "mcp__kb__"),
    "browser": ("浏览器", "网页", "网站", "browser", "mcp__browser__"),
    "blender": ("blender", "三维", "3d", "建模", "mcp__blender__"),
}

_ROUND_UNLOCK = (
    (1, ("ops",)),
    (3, ("media", "net")),
    (5, ("dev",)),
)


def tools_mode():
    """core（默认，只列核心集）/ full（回退到全部工具）"""
    try:
        if str(os.environ.get("WASILY_TOOLS_FULL", "")).strip().lower() in ("1", "true", "yes"):
            return "full"
        if str(_cfg.get("tools.mode", "") or "").strip().lower() == "full":
            return "full"
    except Exception:
        pass
    return "core"


def tool_group(name):
    for g, names in TOOL_GROUPS.items():
        if name in names:
            return g
    return "core"


def enabled_tool_names(query="", mode="", round_no=0, pure_read=False, light_read=False):
    """本轮要展示给模型的工具名。老名字照旧可调用，这里只控制“列不列出来”。
    pure_read=True 时隐藏全部写类工具；light_read=True 时只保留本地读取工具。"""
    q = str(query or "").lower()
    if tools_mode() == "full":
        names = list(TOOLS.keys())
    else:
        names = [n for n in CORE_TOOLS if n in TOOLS]
        groups = set()
        if q:
            for g, kws in GROUP_KEYWORDS.items():
                if any(k.lower() in q for k in kws):
                    groups.add(g)
        # Agent/SOLO 模式：轮次越深逐步放开更多组，避免一上来 98 个挤爆 8k 上下文
        if str(mode).lower() in ("solo", "agent"):
            # Agent 首轮就能截屏/点按/开应用，不必等到第 3 轮才解锁桌面操作
            groups.add("desktop")
            try:
                rn = int(round_no or 0)
            except Exception:
                rn = 0
            for need, gs in _ROUND_UNLOCK:
                if rn >= need:
                    groups.update(gs)
        for g in groups:
            for n in TOOL_GROUPS.get(g, ()):
                if n in TOOLS and n not in names:
                    names.append(n)
    # 无关 MCP 工具不进提示：50 个长描述会让简单问答也在 CPU 上预填数十秒。
    # 这里只控制展示，不修改 TOOLS 注册表，已知名称的 MCP 工具依然可以直接调用。
    try:
        _mcp_max = int(_cfg.get("tools.mcp_max_list", 50) or 50)
    except Exception:
        _mcp_max = 50
    _mcp_names = [n for n in TOOLS
                  if n.startswith("mcp__") and isinstance(TOOLS.get(n), dict)
                  and TOOLS[n].get("_mcp") and n not in names]
    if tools_mode() != "full":
        _related = []
        for n in _mcp_names:
            parts = n.split("__")
            server = parts[1] if len(parts) > 2 else ""
            kws = _MCP_SERVER_KEYWORDS.get(server, (server,))
            if n.lower() in q or any(k and k.lower() in q for k in kws):
                _related.append(n)
        _mcp_names = _related
    for n in _mcp_names[:max(_mcp_max, 0)]:
        names.append(n)
    if light_read and tools_mode() != "full":
        names = [n for n in LIGHT_READ_TOOLS if n in TOOLS]
    if pure_read:
        names = [n for n in names if n not in PURE_READ_HIDDEN_TOOLS]
    # 空工具集会让模型无工具可调（只能空答），直接回退到完整核心集兜底
    if not names:
        names = [n for n in CORE_TOOLS if n in TOOLS] or list(TOOLS.keys())
    return names


def execute_tool(name: str, args: dict, mode=None) -> str:
    """执行工具调用。
    mode 仅保留做兼容：模式限制已在会话层处理（Ask/Plan 自动升级到 Agent + 写操作请求确认），
    这里不再按 ask/plan 直接拒绝写工具，避免出现「只读拦截 + 零工具」的假失败。"""
    if name not in TOOLS:
        _avail = enabled_tool_names()
        return f"未知工具: {name}。本轮可用工具: {', '.join(_avail)}"
    tool = TOOLS[name]
    # 参数校验（最后防线）：校验失败直接返回结构化错误，不执行
    _err = validate_tool_args(name, args)
    if _err:
        _ex = tool_call_example(name)
        return (f"❌ 参数校验失败：{_err}\n"
                f"正确调用示例：```tool_call\n{_ex}\n```")
    try:
        args = _norm_tool_args(name, args)
        _boundary_error = _isolated_tool_error(name, args)
        if _boundary_error:
            return _boundary_error
        if name in _UNDO_TOOLS and not get_workspace_isolated():  # 隔离运行由 Git 清单审计
            try:
                _undo_snapshot(name, args if isinstance(args, dict) else {})
            except Exception:
                pass
        return tool["function"](**args)
    except TypeError as e:
        return f"参数错误: {str(e)}"
    except Exception as e:
        return f"工具执行错误: {str(e)}"


def _agents_list_text() -> str:
    """子智能体清单（供 agent_list 工具与 system prompt 共用）。"""
    try:
        import subagents as _subs
        agents = _subs.list_agents()
    except Exception as e:
        return "子智能体模块不可用: %s" % e
    if not agents:
        return "当前没有可用的子智能体（可在 .wasily/agents/ 下新建 .md 定义）。"
    lines = ["可用子智能体（%d 个）：" % len(agents)]
    for a in agents:
        tools = ", ".join(a["tools"]) if a["tools"] else "核心工具集"
        lines.append("- %s [%s] 最多%d轮：%s｜工具：%s" % (
            a["name"], a["level"], a["max_rounds"],
            a["description"] or "（未写 description）", tools))
    return "\n".join(lines)


def _agent_delegate_stub(name, task=""):
    """兜底：正常情况下 api_server 会在调用 tools.execute_tool 之前拦截并执行委派。"""
    return ("⚠️ agent_delegate 需要在主循环上下文中执行（当前调用未接入委派器）。"
            "请直接使用 file_search / file_read 自行完成。")


def get_tools_prompt(query: str = "", mode: str = "", round_no: int = 0,
                     pure_read: bool = False, light_read: bool = False) -> str:
    """生成工具描述 prompt（默认只列核心集，其余按关键词/模式动态挂载）。
    pure_read=True 时隐藏写类工具；light_read=True 时不附带子智能体等无关说明。"""
    names = enabled_tool_names(query=query, mode=mode, round_no=round_no,
                               pure_read=pure_read, light_read=light_read)
    lines = ["可用工具列表：", ""]
    for name in names:
        tool = TOOLS.get(name)
        if not tool:
            continue
        params = ", ".join(tool["parameters"].keys())
        lines.append(f"- {name}({params}): {tool['description']}")
    hidden = [] if light_read else [n for n in TOOLS if n not in names]
    if hidden:
        lines.append("")
        lines.append("（另有 %d 个桌面/系统/设备/视频类工具本轮未列出，仍可解析调用）" % len(hidden))
    if pure_read:
        lines.append("")
        lines.append("⚠️ 本轮为纯读取任务，**读操作不得产生写入副作用**："
                     "file_write / save_output / file_delete / file_move / file_rename / "
                     "process_start / process_kill / ide_edit_file 等写类工具已禁用，"
                     "禁止把读到的内容写回文件或另存副本。")
    if not light_read:
        try:
            import subagents as _subs
            _sp = _subs.agents_prompt()
            if _sp:
                lines.append("")
                lines.append(_sp)
        except Exception:
            pass
    return "\n".join(lines)


# 启动时自动加载已安装的用户工具
load_user_tools()



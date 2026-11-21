# -*- coding: utf-8 -*-
"""
FreeToken 本地开源模型管理工具
让 AI 听懂「安装 DeepSeek-V4」「部署 GLM-5.2」「列出本地模型」这类指令。
FreeToken 是边缘原生 MoE 推理引擎，能在游戏 PC 上跑 290B+ 前沿开源模型。
GitHub: https://github.com/FlashML-org/FreeToken
"""

import json
import os
import shutil
import subprocess
import urllib.request

SPEC = {
    "name": "freetoken_manager",
    "description": (
        "管理本地开源大模型（基于 FreeToken 引擎）。"
        "支持：检测状态、安装 FreeToken、列出可下载的开源模型、下载模型、部署模型为 OpenAI 兼容 API、停止服务。"
        "当用户说「安装/部署/下载 某个开源模型」「列出本地模型」「启动 FreeToken」时调用本工具。"
    ),
    "parameters": {
        "action": "操作类型：status(检测状态) / install(安装FreeToken) / list(列出支持的开源模型) / download(下载模型) / deploy(部署模型为API服务) / stop(停止服务)",
        "model": "模型 ID，仅 download/deploy 时需要。如 deepseek-v4-flash、qwen3.6-35b-a3b、glm-5.2",
        "port": "部署端口，默认 8100（避免与 Wasily 后端 8000 冲突）",
    },
}

# FreeToken 支持的前沿开源 MoE 模型清单（与官方 README 对齐）
# size 为大致权重体积，实际随量化格式变化；vram 为建议显存
_CATALOG = [
    {
        "id": "deepseek-v4-flash",
        "name": "DeepSeek-V4-Flash",
        "repo": "deepseek-ai/DeepSeek-V4-Flash",
        "params": "290B+ (MoE)",
        "size_gb": 160,
        "vram_gb": 24,
        "desc": "深度求索前沿 MoE，CPU-GPU 协同可在游戏 PC 跑",
    },
    {
        "id": "qwen3.6-35b-a3b",
        "name": "Qwen3.6-35B-A3B",
        "repo": "Qwen/Qwen3.6-35B-A3B",
        "params": "35B (MoE, 激活3B)",
        "size_gb": 20,
        "vram_gb": 12,
        "desc": "阿里通义千问 MoE，激活参数小、速度快",
    },
    {
        "id": "glm-5.2",
        "name": "GLM-5.2",
        "repo": "THUDM/glm-5.2",
        "params": "未公开 (MoE)",
        "size_gb": 40,
        "vram_gb": 16,
        "desc": "智谱 GLM 最新开源版，中文能力强",
    },
]

# FreeToken 服务默认端口（避开 Wasily 后端 8000）
_DEFAULT_PORT = 8100
# 状态文件：记录当前部署的模型与端口，便于跨调用查询
_STATE_FILE = os.path.join(os.path.expanduser("~"), ".wasily", "freetoken_state.json")


def _load_state():
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_state(state):
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _is_installed():
    """FreeToken 是否已通过 pip 安装"""
    return shutil.which("freetoken") is not None


def _is_running(port):
    """检测 FreeToken 服务是否在指定端口响应 OpenAI 兼容 /v1/models"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _find_model(model_id):
    mid = (model_id or "").strip().lower()
    for m in _CATALOG:
        if m["id"] == mid or m["name"].lower() == mid or m["repo"].lower() == mid:
            return m
    # 模糊匹配：用户可能只说 "deepseek" / "qwen" / "glm"
    for m in _CATALOG:
        if mid and mid in m["id"]:
            return m
    return None


def _do_status(port):
    installed = _is_installed()
    state = _load_state()
    cur_port = int(state.get("port", port or _DEFAULT_PORT))
    running = _is_running(cur_port) if installed else False
    lines = [
        f"FreeToken 已安装: {'是' if installed else '否'}",
        f"服务运行中: {'是 (端口 ' + str(cur_port) + ')' if running else '否'}",
    ]
    if state.get("model"):
        lines.append(f"当前部署模型: {state['model']}")
    if running:
        lines.append(f"OpenAI 兼容接口: http://127.0.0.1:{cur_port}/v1")
    if not installed:
        lines.append("提示：用 action=install 一键安装 FreeToken")
    return "\n".join(lines)


def _do_install():
    if _is_installed():
        return "FreeToken 已安装，无需重复安装。可用 action=list 查看可下载模型。"
    # 优先 uv（更快），回退 pip
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "pip", "install", "freetoken[accel]"]
    else:
        cmd = ["pip", "install", "freetoken[accel]"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0:
            return "FreeToken 安装成功。现在可以用 action=list 查看可下载模型，action=download 下载。"
        return f"安装失败（exit {proc.returncode}）：\n{proc.stdout}\n{proc.stderr}"
    except subprocess.TimeoutExpired:
        return "安装超时（10分钟）。网络可能较慢，可手动执行：pip install freetoken[accel]"
    except Exception as e:
        return f"安装异常：{e}"


def _do_list():
    lines = ["可下载的开源模型（FreeToken 引擎，本地运行，完全免费）：", ""]
    for m in _CATALOG:
        lines.append(f"• {m['name']}  (id: {m['id']})")
        lines.append(f"  参数量: {m['params']}  |  权重约 {m['size_gb']}GB  |  建议显存 {m['vram_gb']}GB+")
        lines.append(f"  {m['desc']}")
        lines.append("")
    lines.append("下载：action=download, model=<id>")
    lines.append("部署：action=deploy, model=<id>  （下载+启动API服务一步完成）")
    return "\n".join(lines)


def _do_download(model):
    m = _find_model(model)
    if not m:
        return f"未识别模型「{model}」。用 action=list 查看支持的模型 id。"
    if not _is_installed():
        return "FreeToken 未安装。先用 action=install 安装。"
    # FreeToken CLI 下载子命令（与官方 docs/cli.md 对齐；若子命令名不同需调整）
    cmd = ["freetoken", "download", m["repo"]]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if proc.returncode == 0:
            return f"模型 {m['name']} 下载完成。可用 action=deploy, model={m['id']} 部署为 API 服务。"
        return f"下载失败（exit {proc.returncode}）：\n{proc.stdout}\n{proc.stderr}"
    except subprocess.TimeoutExpired:
        return f"下载超时（{m['name']} 约 {m['size_gb']}GB，大模型需更长时间）。可手动执行：{' '.join(cmd)}"
    except Exception as e:
        return f"下载异常：{e}"


def _do_deploy(model, port):
    m = _find_model(model)
    if not m:
        return f"未识别模型「{model}」。用 action=list 查看支持的模型 id。"
    if not _is_installed():
        return "FreeToken 未安装。先用 action=install 安装。"
    port = int(port or _DEFAULT_PORT)
    if _is_running(port):
        return f"端口 {port} 已有服务在运行。可先 action=stop 停止，或换 port 参数。"
    # 后台启动 FreeToken 服务（OpenAI 兼容 API）
    cmd = ["freetoken", "serve", m["repo"], "--port", str(port)]
    try:
        # Windows 下用 CREATE_NEW_PROCESS_GROUP 让子进程独立；输出重定向到日志
        log_dir = os.path.join(os.path.expanduser("~"), ".wasily", "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"freetoken_{m['id']}_{port}.log")
        lf = open(log_path, "a", encoding="utf-8")
        proc = subprocess.Popen(
            cmd, stdout=lf, stderr=lf,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        _save_state({"model": m["id"], "port": port, "pid": proc.pid, "log": log_path})
        return (
            f"已启动 {m['name']} 部署（PID {proc.pid}）。模型加载需要几分钟，"
            f"日志：{log_path}\n"
            f"就绪后 OpenAI 兼容接口：http://127.0.0.1:{port}/v1\n"
            f"在 Wasily 设置中心 → 模型 → 添加模型，Base URL 填上面的地址即可切换到本地大模型。"
        )
    except Exception as e:
        return f"部署异常：{e}"


def _do_stop():
    state = _load_state()
    pid = state.get("pid")
    if not pid:
        return "没有记录到运行中的 FreeToken 服务。"
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            os.kill(pid, 15)
        _save_state({})
        return f"已停止 FreeToken 服务（PID {pid}）。"
    except Exception as e:
        return f"停止失败：{e}"


def run(action="status", model="", port=0):
    action = (action or "status").strip().lower()
    port = int(port) if port else _DEFAULT_PORT
    if action == "status":
        return _do_status(port)
    if action == "install":
        return _do_install()
    if action == "list":
        return _do_list()
    if action == "download":
        return _do_download(model)
    if action == "deploy":
        return _do_deploy(model, port)
    if action == "stop":
        return _do_stop()
    return f"未知 action「{action}」。支持：status / install / list / download / deploy / stop"

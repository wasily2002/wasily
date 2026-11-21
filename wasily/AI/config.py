"""
wasily 配置中心
单例加载 config.json；提供 get()/set()/save()；首次运行自动生成默认配置。
所有模块统一从这里读配置，修改 config.json 后调用 reload() 立即生效。
"""
import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

_defaults = {
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
        "model_path": os.path.join(BASE_DIR, "models_7b",
                                   "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf"),
        "n_ctx": 8192,
        "max_generate_tokens": 512,
        "n_gpu_layers": -2,          # -2=自动按显存计算(推荐) 0=纯CPU >0=指定层数
        "n_threads": 0,              # 0=自动(cpu_count)
    },
    "models": [
        {
            "id": "local",
            "name": "本地 Qwen2.5-7B (GGUF)",
            "provider": "llama",
            "path": "",    # 空则用 server.model_path
            "type": "chat"
        },
        {
            "id": "local-large",
            "name": "本地 14B 模型（需自行放入 models_14b/）",
            "provider": "llama",
            "path": os.path.join(BASE_DIR, "models_14b", "qwen2.5-14b-instruct-q4_k_m.gguf"),
            "type": "chat"
        },
        {
            "id": "remote-openai",
            "name": "远程 OpenAI API (gpt-4o-mini)",
            "provider": "openai",
            "url": "https://api.openai.com/v1",
            "api_key": "",
            "model_name": "gpt-4o-mini",
            "type": "chat"
        },
        {
            "id": "remote-deepseek",
            "name": "远程 DeepSeek API",
            "provider": "openai",
            "url": "https://api.deepseek.com/v1",
            "api_key": "",
            "model_name": "deepseek-chat",
            "type": "chat"
        }
    ],
    "current_model": "local",
    "ai_name": "wasily（瓦西里）",
    "resource": {                     # 批2 资源管理器
        "enabled": True,
        "sample_interval": 1.0,       # 负载采样周期(秒)
        "hysteresis_sec": 3,          # 状态切换防抖(秒)
        "cpu_infer_level": 70,        # 推理中且cpu>此值 → S2高载
        "cpu_conflict_level": 85,     # 推理中且cpu>此值 → S3冲突(隐藏动画)
        "mem_level": 85,              # 内存>此值 → S2高载
        "level0": {"fps": 60, "particles": 1.0},
        "level1": {"fps": 30, "particles": 0.6},
        "level2": {"fps": 15, "particles": 0.3},
        "level3": {"mode": "hidden"},
        "manual_pause": False,        # 用户手动暂停(优先级最高)
    },
    "tts": {
        "voice": "zh-CN-XiaoxiaoNeural",
        "rate": "+0%",
        "fallback_sapi": True,        # edge-tts失败回退Windows离线语音
    },
    "voice": {
        "shortcut_enabled": True,     # Alt+V 按住说话，松开转文字
    },
    "ui": {
        "opacity": 1.0,               # 0.1~1.0 窗口透明度
        "radius": 24,                 # 圆角像素
        "avatar_on_top": True,
        "theme": "dark",              # dark/light/system
        "language": "zh-CN",          # zh-CN/en-US，设置中心与面板语言
        "send_key": "enter",          # enter/ctrl-enter
        "page_open_mode": "embedded", # 本地链接打开方式
        "ai_watermark": True,
    },
    "ide": {
        "startup_window": "system",   # system/new/restore/none
        "close_last_window": "quit",  # quit/keep（keep=收进托盘后台常驻）
        "markdown_open": "editor",    # editor/preview
    },
    "network": {
        "proxy": "system",            # system/none/custom
        "proxy_url": "",              # custom 时生效，如 http://127.0.0.1:7890
    },
    "monitor": {
        "port": 9101,
        "sample_hz": 1,               # 采样频率(次/秒)
        "retention_days": 7,          # 历史数据保留天数
    },
    "logging": {
        "path": "",                   # 空=默认 %APPDATA%/Wasily/logs；填绝对路径则改存到该目录
        "keep_days": 30,              # 历史会话目录保留天数（0=不清理）
    },
    "update": {
        "channel": "stable",          # stable/beta
        "manifest_url": "",           # 空=读取 updates/latest.json；可填局域网 http 地址
        "auto_check": True,
        "last_check": 0,
        "last_version": "",
    },
    "web": {
        "auto_search": True,          # 公司/时效类提问自动联网检索后再回答
        "engine": "bing",             # bing/baidu
        "count": 6,
        "timeout": 10,                # 单次检索总超时(秒)
        "cache_ttl": 600,             # 结果缓存(秒)
    },
    "prompt_optimize": {
        "mode": "basic",              # basic=规则即时 / llm=本地模型改写 / off=关闭
        "template": "",               # 高级：自定义优化系统提示词（留空用内置）
        "max_chars": 1200,
    },
    "task_router": {
        "auto": True,                 # 自动匹配技能/智能体
        "preflight_notice": True,     # 执行前告知依赖与权限
    },
    "train": {
        "rounds": 3,                  # 默认训练轮数
        "turns_per_topic": 4,         # 每子话题自对话轮数
        "eval_threshold": 4,          # 评估>=该分数才写入learnings
        "max_seconds_per_round": 600, # 单轮限时
    },
    "memory": {
        "cross_session": False,       # 原始跨会话历史摘要仍保持独立开关
        "vector": {
            "enabled": True,
            "inject": True,
            "model": "BAAI/bge-small-zh-v1.5",
            "top_k": 5,
            "inject_top": 3,
            "min_score": 0.45,
            "dedupe": 0.96,
            "batch_size": 16,
        },
    },
    "kb": {
        # 本地知识库：默认只读扫描 C:/D: 全盘（自动跳过系统与缓存目录）
        "backend": "local",              # local=内置实现；khoj=复用 Khoj 服务
        "roots": ["C:\\", "D:\\"],
        "exclude": [],                   # 额外排除的绝对路径前缀
        "max_file_mb": 20,
        "max_text_chars": 200000,
        "max_pdf_pages": 60,
        "max_sheet_rows": 2000,
        "max_chunk_chars": 700,
        "chunk_overlap": 100,
        "max_db_gb": 3.0,
        "image": {"caption": True, "caption_max": 200},  # 图片除 OCR 外是否用 SmolVLM 生成描述
        "index": {"batch": 16, "sleep_ms": 0},
        "khoj": {"url": "http://127.0.0.1:42110"},
    },
    "sources": [
        # 统一存储适配器：local / wsl / ssh / webdav / s3
        # 示例（默认不启用，按需替换 id 与 options 后把 enabled 设为 true）：
        # {"id": "wsl-ubuntu", "type": "wsl", "name": "WSL Ubuntu",
        #  "enabled": false, "options": {"distro": "Ubuntu"}},
        # {"id": "nas", "type": "webdav", "name": "团队云盘",
        #  "enabled": false, "options": {"base_url": "https://dav.example.com/",
        #                                "username": "", "password": "", "root": "/"}},
    ],
    "apps": {
        # 内置应用（Office/视频/代码）允许操作的根目录；空=当前工作区
        "roots": [],
        "backup": True,              # 写入前备份到 outputs/app_backups
    },
    "browser": {
        "cdp_port": 9222,            # Chrome/Edge 远程调试端口
        "executable": "",            # 留空自动探测 Edge/Chrome
        "auto_launch": True,         # 未检测到调试端口时自动拉起浏览器
        "allow_eval": True,          # 允许 browser_eval 执行页面脚本
    },
    "mcp": {
        "gateway_token": "",         # 空=仅本机可访问 /mcp；设置后需 Bearer 令牌
    },
    "training": {
        "auto_collect": True,        # kb 服务启动后自动增量采集历史会话
        "interval_sec": 600,
        "min_chars": 8,
        "max_chars": 20000,
        "val_ratio": 0.1,
    },
    "devices": {
        "enabled": True,
        "scan_timeout": 3,
    },
    "install_tool": {
        "allow": True,
        "allow_confirm": True,        # 敏感工具操作需用户在对话窗口点击批准
        "confirm_timeout": 180,       # 确认等待超时(秒)，超时视为拒绝
        "whitelist": ["psutil", "requests", "pillow", "numpy", "pilk", "rembg",
                      "bleak", "pyserial", "faster-whisper", "jieba", "websockets",
                      "edge-tts", "openai-whisper", "sounddevice", "pyaudio",
                      "pypdf", "openpyxl", "python-docx", "python-pptx", "pytesseract",
                      "playwright", "selenium", "beautifulsoup4", "pandas",
                      "matplotlib", "qrcode", "pyyaml", "pypinyin", "yagmail", "markdown"],
    },
}

_lock = threading.Lock()
_cache = None


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(force=False):
    """加载配置（带缓存）。config.json 缺失或字段不全时自动补默认值。"""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        data = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        merged = _deep_merge(_defaults, data)
        _cache = merged
        # 首次或字段升级时把默认补齐写回
        if not os.path.exists(CONFIG_PATH) or data != merged:
            _save_unlocked(merged)
        return _cache


def _save_unlocked(data):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def get(path="", default=None):
    """按点路径取配置，如 cfg.get('resource.cpu_conflict_level')"""
    cfg = load()
    if not path:
        return cfg
    cur = cfg
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def set_value(path, value, save=True):
    """按点路径写配置"""
    cfg = load()
    parts = path.split(".")
    with _lock:
        cur = cfg
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
        if save:
            _save_unlocked(cfg)
    return value


def save():
    with _lock:
        if _cache is not None:
            _save_unlocked(_cache)


def reload():
    return load(force=True)


if __name__ == "__main__":
    print(json.dumps(load(), ensure_ascii=False, indent=2)[:500])

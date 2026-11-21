"""
资源管理器（对话优化核心）：按实时系统负载动态分级大脑动画策略
状态机（优先级：手动暂停 > S3冲突 > S2高载 > S1推理中 > S0空闲）：
  S0 空闲   : 60fps 全粒子
  S1 推理中 : 30fps 60%粒子
  S2 高载   : cpu>70% 或 mem>85% → 15fps 30%粒子
  S3 冲突   : 推理中 且 cpu>85% → 8fps 30%粒子 降级渲染（思考反馈不中断）
策略经 /agent_status 下发给 brain.html / agent.html；阈值可在 config.json 配置。
"""
import threading
import time

import psutil

try:
    import config as _cfg
except Exception:
    _cfg = None

# 当前资源状态（供 /agent_status 合并下发）
RESOURCE_STATE = {
    "level": "S0",        # S0/S1/S2/S3/manual
    "reason": "初始化",
    "fps": 60,
    "particles": 1.0,
    "hidden": False,
    "ts": 0,
}
_state_lock = threading.Lock()
_thread = None


def _conf(path, default):
    return _cfg.get("resource." + path, default) if _cfg else default


def _policy_for(level):
    cfg = (_cfg.get("resource") if _cfg else {}) or {}
    if level == "manual":
        return {"fps": 0, "particles": 0.0, "hidden": True, "paused": True}
    if level == "S3":
        # 推理冲突时降级渲染（低帧率+少粒子），绝不隐藏：
        # 思考状态正是大脑动画的核心反馈时刻，hidden 会让大脑在思考时消失
        p = cfg.get("level3") or {}
        return {"fps": p.get("fps", 8), "particles": p.get("particles", 0.3),
                "hidden": False, "paused": False}
    key = {"S0": "level0", "S1": "level1", "S2": "level2"}[level]
    p = cfg.get(key) or {"fps": 60, "particles": 1.0}
    return {"fps": p.get("fps", 60), "particles": p.get("particles", 1.0),
            "hidden": False, "paused": False}


def _raw_level(cpu, mem, busy):
    c_conflict = _conf("cpu_conflict_level", 85)
    c_infer = _conf("cpu_infer_level", 70)
    m_level = _conf("mem_level", 85)
    if busy and cpu >= c_conflict:
        return "S3", f"推理中+CPU {cpu:.0f}%≥{c_conflict}%"
    if cpu >= c_infer or mem >= m_level:
        return "S2", f"高载 CPU {cpu:.0f}% / MEM {mem:.0f}%"
    if busy:
        return "S1", "模型推理中"
    return "S0", "空闲"


def _loop(agent_state_ref):
    interval = float(_conf("sample_interval", 1.0))
    hysteresis = float(_conf("hysteresis_sec", 3))
    pending, since = None, 0.0
    while True:
        try:
            st = agent_state_ref()
            busy = st.get("state") == "thinking"
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory().percent
            manual = bool(_conf("manual_pause", False))
            raw, reason = ("manual", "用户手动暂停") if manual else _raw_level(cpu, mem, busy)
            now = time.time()
            with _state_lock:
                cur = RESOURCE_STATE["level"]
            # 防抖：S3/manual 立即切换，其余需持续 hysteresis 秒
            if raw != cur:
                if raw in ("S3", "manual"):
                    pending, since = raw, 0.0
                    new_level = raw
                else:
                    if pending == raw:
                        if now - since >= hysteresis:
                            new_level = raw
                        else:
                            new_level = None
                    else:
                        pending, since = raw, now
                        new_level = None
            else:
                pending = None
                new_level = None
            if new_level:
                pol = _policy_for(new_level)
                with _state_lock:
                    RESOURCE_STATE.update(level=new_level, reason=reason, ts=now, **pol)
            time.sleep(interval)
        except Exception as e:
            print("[resource] 采样异常:", e, flush=True)
            time.sleep(2)


def start_resource_manager(agent_state_ref=lambda: {"state": "idle"}):
    """在 api_server 进程内启动 1s 采样线程"""
    global _thread
    if not (_cfg.get("resource.enabled", True) if _cfg else True):
        print("[resource] 资源管理器已在 config 中禁用", flush=True)
        return
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, args=(agent_state_ref,),
                               daemon=True, name="resource-manager")
    _thread.start()
    print("[resource] 资源管理器已启动 (1s 采样 → 动画分级 S0~S3)", flush=True)


def get_resource_state():
    with _state_lock:
        return dict(RESOURCE_STATE)


def set_manual_pause(paused):
    """手动暂停开关（写 config 并立即生效）"""
    if _cfg:
        _cfg.set_value("resource.manual_pause", bool(paused))
    return bool(paused)

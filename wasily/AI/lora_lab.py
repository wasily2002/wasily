# -*- coding: utf-8 -*-
"""
LoRA 微调实验室（三期）
流程：export_dataset() 从加密历史/记忆提炼训练集 → train_start() 启动训练引擎
      → apply() 挂载 adapter 到 config（lora.path），重启后 _load_llm 自动加载
引擎优先级：llama.cpp finetune CLI（直接产出 GGUF adapter，可被 llama-cpp 加载）
          → transformers+peft 脚本（需 HF 格式模型，产出后需 convert-lora-to-gguf 转换）
"""
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from secure_store import EncLines

_DIR = Path(__file__).resolve().parent
LAB = _DIR / "finetune"
LAB.mkdir(exist_ok=True)
STATE_PATH = LAB / "state.json"
DATASET_JSONL = LAB / "dataset.jsonl"
DATASET_VAL = LAB / "dataset_val.jsonl"
CORPUS_TXT = LAB / "corpus.txt"
TRAIN_LOG = LAB / "train.log"
HISTORY_PATH = LAB / "history.json"
VOICE_DIR = _DIR / "_archive" / "voice_samples"

# 完善训练系统（四期）数据文件
FEEDBACK_PATH = LAB / "feedback.jsonl"      # 用户纠错样本池
KNOWLEDGE_PATH = LAB / "knowledge.jsonl"     # 知识注入样本池
DELETED_PATH = LAB / "deleted_samples.json"  # 被用户删除的样本指纹
AUTOTRAIN_CFG = LAB / "autotrain.json"       # 自动训练配置

_PROC = {"p": None}
_VPROC = {"p": None}   # 声音微调子进程
_LOCK = threading.Lock()

VSTATE_PATH = LAB / "voice_state.json"
VTRAIN_LOG = LAB / "voice_train.log"


def _vstate_load() -> dict:
    if VSTATE_PATH.exists():
        try:
            return json.loads(VSTATE_PATH.read_text("utf-8"))
        except Exception:
            pass
    return {}


def _vstate_save(**kv):
    st = _vstate_load()
    st.update(kv)
    VSTATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")


def voice_status() -> dict:
    st = _vstate_load()
    # 自愈：状态是 training 但训练进程已死（服务重启/机器重启会杀掉子进程）→ 标记 error
    if st.get("status") == "training" and st.get("pid"):
        try:
            os.kill(int(st["pid"]), 0)
        except (OSError, ProcessLookupError):
            _vstate_save(status="error", finished=time.time(),
                         message="训练进程已中断（服务重启导致），请重新点「开始训练」")
            st = _vstate_load()
    # 从日志补充最新进度
    if st.get("status") in ("training", "done", "error") and VTRAIN_LOG.exists():
        try:
            lines = VTRAIN_LOG.read_text("utf-8", errors="replace").splitlines()[-400:]
            for ln in reversed(lines):
                m = re.search(r"\[vtrain\] step (\d+)/(\d+) loss=([\d.]+)(?: valloss=([\d.]+))?", ln)
                if m:
                    st["step"] = int(m.group(1)); st["total_steps"] = int(m.group(2))
                    st["loss"] = float(m.group(3))
                    if m.group(4):
                        st["valloss"] = float(m.group(4))
                    break
            # 样本规模：切段数 / 有效音频总时长（声音训练不消耗 LLM token，用音频量代替）
            for ln in lines:
                m = re.search(r"转写完成: (\d+) 段有效，总时长 (\d+)s", ln)
                if m:
                    st["segs"] = int(m.group(1)); st["audio_sec"] = int(m.group(2))
                    break
            st["log_tail"] = [l for l in lines if l.strip()][-12:]
        except Exception:
            pass
    # 派生进度指标：完成百分比 / 已运行时长 / 预计剩余时间(ETA)
    if st.get("started"):
        st["elapsed"] = max(0, int((st.get("finished") or time.time()) - st["started"]))
    sp, tot = st.get("step"), st.get("total_steps")
    if sp and tot:
        st["percent"] = round(sp / tot * 100, 1)
        if st.get("status") == "training" and st.get("elapsed") and sp > 0:
            st["eta"] = int(st["elapsed"] / sp * (tot - sp))
    return st


def voice_finetune_start(epochs=30, lr=1e-5, max_steps=0, force=False) -> dict:
    """启动 F5-TTS 离线权重微调（非参考音校准）。返回立即，后台跑。"""
    st = voice_status()
    if st.get("status") == "training":
        return {"ok": False, "message": "声音训练已在进行中"}
    exts = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}
    samples = [f for f in os.listdir(VOICE_DIR)
               if Path(f).suffix.lower() in exts and not f.startswith("ref_speech")]
    if len(samples) < 1:
        return {"ok": False, "message": "没有可用的声音样本，请先在「声音样本」区上传"}
    if len(samples) < 2 and not force:
        return {"ok": False, "message": "仅 1 段样本，微调容易过拟合；建议 ≥2 段（或用 force=1 强制）"}
    out_dir = LAB / f"voice_ckpt_{time.strftime('%m%d_%H%M')}"
    cmd = [sys.executable, str(LAB / "voice_finetune.py"),
           "--samples_dir", str(VOICE_DIR), "--out", str(out_dir),
           "--epochs", str(int(epochs)), "--lr", str(lr)]
    if max_steps:
        cmd += ["--max_steps", str(int(max_steps))]
    VTRAIN_LOG.write_text("", "utf-8")
    _log("启动声音微调: " + " ".join(cmd))
    env = dict(os.environ)
    env.pop("PYTHONHASHSEED", None)
    try:
        p = subprocess.Popen(cmd, cwd=str(LAB), env=env,
                             stdout=open(VTRAIN_LOG, "a", encoding="utf-8"),
                             stderr=subprocess.STDOUT)
    except Exception as e:
        return {"ok": False, "message": f"启动失败: {e}"}
    _VPROC["p"] = p
    started = time.time()
    _vstate_save(status="training", pid=p.pid, out=str(out_dir), started=started,
                 epochs=int(epochs), lr=lr, samples=len(samples),
                 message=f"声音微调启动（{len(samples)} 段样本，CPU 离线训练）")

    def _watch():
        rc = p.wait()
        ckpt = out_dir / "model_best.safetensors"
        ok = rc == 0 and ckpt.exists()
        steps = 0; best_val = None; best_step = 0; segs = 0
        try:
            for ln in VTRAIN_LOG.read_text("utf-8", errors="replace").splitlines():
                m = re.search(r"\[vtrain\] step (\d+)/(\d+)", ln)
                if m: steps = int(m.group(1))
                m = re.search(r"\[vtrain\] DONE steps=(\d+) best_step=(\d+) best_valloss=([\d.]+)", ln)
                if m:
                    steps = int(m.group(1)); best_step = int(m.group(2)); best_val = float(m.group(3))
                m = re.search(r"\[vtrain\] 转写完成: (\d+) 段", ln)
                if m: segs = int(m.group(1))
        except Exception:
            pass
        fin = time.time()
        if ok:
            import config as _cfg
            _cfg.load()
            _cfg.set_value("voice.f5_ckpt", str(ckpt))
            msg = (f"声音微调完成：{segs} 段样本训练 {steps} 步，"
                   f"最佳验证 loss {best_val}（第 {best_step} 步）。"
                   "已自动切换为微调后的声音模型，视频通话/对话即刻生效。")
            _vstate_save(status="done", finished=fin, message=msg,
                         ckpt=str(ckpt), steps=steps, best_valloss=best_val)
        else:
            tail = ""
            try:
                tail = "\n".join(VTRAIN_LOG.read_text("utf-8", errors="replace").splitlines()[-3:])
            except Exception:
                pass
            msg = f"声音微调失败（退出码 {rc}）。{tail[:300]}"
            _vstate_save(status="error", finished=fin, message=msg)
        _hist_add({"type": "voice_finetune", "adapter": out_dir.name,
                   "started": started, "finished": fin, "samples": len(samples),
                   "steps": steps, "final_loss": best_val, "tokens": 0,
                   "status": "done" if ok else "error", "message": msg})
        _log(f"声音微调结束 rc={rc} steps={steps} ok={ok}")
    threading.Thread(target=_watch, daemon=True).start()
    return {"ok": True, "out": out_dir.name}


def voice_finetune_stop() -> dict:
    p = _VPROC.get("p")
    if p and p.poll() is None:
        p.kill()
        _vstate_save(status="idle", message="已手动停止")
        return {"ok": True}
    return {"ok": False, "message": "无运行中的声音训练"}


# ---------- 训练历史（持久化，每次训练一条） ----------
def _hist_add(rec: dict):
    with _LOCK:
        lst = history()
        lst.append(rec)
        HISTORY_PATH.write_text(json.dumps(lst[-200:], ensure_ascii=False, indent=1),
                                "utf-8")


def history() -> list:
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text("utf-8"))
        except Exception:
            pass
    return []


def _md5_file(fp) -> str:
    h = hashlib.md5()
    with open(fp, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _voice_set_hash() -> tuple:
    """当前声音样本集指纹：(hash, [md5...], 文件数)。排除克隆参考副本。"""
    exts = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}
    md5s = []
    if VOICE_DIR.exists():
        for f in sorted(os.listdir(VOICE_DIR)):
            if f == "ref_speech_10s.wav" or Path(f).suffix.lower() not in exts:
                continue
            try:
                md5s.append(_md5_file(VOICE_DIR / f))
            except Exception:
                pass
    combo = hashlib.md5(("|".join(sorted(md5s))).encode()).hexdigest() if md5s else ""
    return combo, md5s, len(md5s)


# ---------- 状态 ----------
def _save_state(**kv):
    st = status()
    st.update(kv)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")


def status() -> dict:
    st = None
    if STATE_PATH.exists():
        try:
            st = json.loads(STATE_PATH.read_text("utf-8"))
        except Exception:
            pass
    if not st:
        st = {"status": "idle", "samples": 0, "adapter": "", "started": 0,
              "finished": 0, "engine": "", "message": ""}
    # 状态对账：training 但进程已死 → 看日志收尾标记
    if st.get("status") == "training":
        pid = st.get("pid")
        alive = False
        if pid:
            try:
                import ctypes
                h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
                alive = bool(h)
                if h:
                    ctypes.windll.kernel32.CloseHandle(h)
            except Exception:
                pass
        if not alive:
            tail = "\n".join(tail_log(5))
            ad = st.get("adapter", "")
            if "[train] DONE" in tail and ad and (Path(ad) / "adapter_config.json").exists():
                st.update(status="done", finished=time.time(),
                          message=f"训练完成，adapter: {Path(ad).name}")
            else:
                st.update(status="error", finished=time.time(),
                          message="训练进程已退出但未产出 adapter，查看 train.log（可能内存不足）")
    # 实时训练指标透传：从日志尾部解析 step 进度 / 累计 tokens / 最新 loss
    if st.get("status") in ("training", "done", "error"):
        for ln in reversed(tail_log(40)):
            m = re.search(r"\[train\] step (\d+)/(\d+) loss=([\d.]+) tokens=(\d+)", ln)
            if m:
                st["step"], st["steps_total"] = int(m.group(1)), int(m.group(2))
                st["loss"], st["tokens"] = float(m.group(3)), int(m.group(4))
                break
            m = re.search(r"\[train\] DONE tokens=(\d+)", ln)
            if m:
                st["tokens"] = int(m.group(1))
                break
    # 派生进度指标：完成百分比 / 已运行时长 / 预计剩余时间(ETA)
    if st.get("started"):
        st["elapsed"] = max(0, int((st.get("finished") or time.time()) - st["started"]))
    sp, tot = st.get("step"), st.get("steps_total")
    if sp and tot:
        st["percent"] = round(sp / tot * 100, 1)
        if st.get("status") == "training" and st.get("elapsed") and sp > 0:
            st["eta"] = int(st["elapsed"] / sp * (tot - sp))
    return st


def _log(line: str):
    with open(TRAIN_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {line}\n")


def tail_log(n=30) -> list:
    if not TRAIN_LOG.exists():
        return []
    return TRAIN_LOG.read_text("utf-8", errors="ignore").splitlines()[-n:]


# ---------- 数据集导出 ----------
def export_dataset(max_samples=500) -> dict:
    """从 history.enc 提炼高质量问答对 + L3 核心认知，输出 Alpaca JSONL 与 ChatML 语料"""
    hist = EncLines(str(_DIR / "history.enc")).load_all()
    samples = []
    seen = set()
    dropped_dup = 0
    deleted = _load_deleted()
    for r in hist:
        if r.get("type") != "chat":
            continue
        q = (r.get("content") or "").strip()
        a = (r.get("reply") or "").strip()
        if len(q) < 4 or len(a) < 30 or a.startswith("生成出错"):
            continue
        if "```plan" in a:  # 计划 JSON 不进入训练
            continue
        # 去重：同一问+答只保留一次（重复样本会让模型背答案而不是学语气）
        key = hashlib.md5((q + "\u0001" + a).encode("utf-8")).hexdigest()
        if key in deleted:
            continue
        if key in seen:
            dropped_dup += 1
            continue
        seen.add(key)
        samples.append({"instruction": q[:800], "input": "", "output": a[:1500],
                        "tool": bool(r.get("used_tool"))})
    # 工具调用成功的样本权重更高：放前面
    samples.sort(key=lambda s: -s["tool"])
    samples = samples[:max_samples]
    # L3 核心认知 → 自我认知训练样本（最多 3 条，防膨胀淹没对话样本）
    try:
        import memory_net
        n_l3 = 0
        for r in memory_net._LAYERS.load_all():
            if r.get("layer") == 3 and r.get("text"):
                sample = {"instruction": "介绍一下你自己和你掌握的情况",
                          "input": "", "output": r["text"][:400], "tool": False}
                key = hashlib.md5((sample["instruction"] + "\u0001" +
                                   sample["output"]).encode("utf-8")).hexdigest()
                if key not in seen:
                    seen.add(key)
                    samples.insert(0, sample)
                n_l3 += 1
                if n_l3 >= 3:
                    break
    except Exception:
        pass

    # 合并用户反馈样本（点赞/点踩修正）和知识注入样本
    extra = []
    if FEEDBACK_PATH.exists():
        for ln in FEEDBACK_PATH.read_text("utf-8").splitlines():
            try:
                f = json.loads(ln)
                if f.get("vote") == "up" and f.get("original"):
                    extra.append({"instruction": f["question"][:800], "input": "",
                                  "output": f["original"][:1500], "tool": False,
                                  "source": "feedback_up"})
                elif f.get("vote") == "down" and f.get("corrected"):
                    extra.append({"instruction": f["question"][:800], "input": "",
                                  "output": f["corrected"][:1500], "tool": False,
                                  "source": "feedback_down"})
            except Exception:
                pass
    if KNOWLEDGE_PATH.exists():
        for ln in KNOWLEDGE_PATH.read_text("utf-8").splitlines():
            try:
                k = json.loads(ln)
                if k.get("text"):
                    src = k.get("source", "")[:20]
                    extra.append({"instruction": f"介绍一下关于{src}的情况" if src else "介绍一下这方面的情况",
                                  "input": "", "output": k["text"][:1200], "tool": False,
                                  "source": "knowledge"})
            except Exception:
                pass
    # 合并微信对话学习样本
    wechat_ds = _DIR / "training_data" / "wechat_learn.jsonl"
    if wechat_ds.exists():
        for ln in wechat_ds.read_text("utf-8").splitlines():
            try:
                w = json.loads(ln)
                if w.get("instruction") and w.get("output"):
                    extra.append({"instruction": w["instruction"][:800], "input": "",
                                  "output": w["output"][:1500], "tool": False,
                                  "source": w.get("source", "wechat")})
            except Exception:
                pass
    # 去重后并入
    for s in extra:
        key = hashlib.md5((s["instruction"] + "\u0001" + s["output"]).encode("utf-8")).hexdigest()
        if key not in seen:
            seen.add(key)
            samples.append(s)

    # 训练/验证集划分（固定种子：同一份数据每次划分一致，评估可比）
    rs = samples[:]
    random.Random(42).shuffle(rs)
    n_val = max(1, int(len(rs) * 0.1)) if len(rs) >= 10 else 0
    val = rs[:n_val]
    train = rs[n_val:] if n_val else rs
    # 验证集保持「工具样本优先」的顺序无意义，按原顺序写回
    def _dump(path, rows):
        with open(path, "w", encoding="utf-8") as f:
            for s in rows:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
    _dump(DATASET_JSONL, train)
    _dump(DATASET_VAL, val)
    # ChatML 纯文本语料（供 llama.cpp finetune 使用）
    with open(CORPUS_TXT, "w", encoding="utf-8") as f:
        for s in train + val:
            f.write(f"<|im_start|>user\n{s['instruction']}<|im_end|>\n"
                    f"<|im_start|>assistant\n{s['output']}<|im_end|>\n")
    ds_hash = _md5_file(DATASET_JSONL) if train else ""
    avg_len = round(sum(len(s["output"]) for s in train) / max(1, len(train)), 1)
    _save_state(status="exported", samples=len(train), val_samples=len(val),
                dropped_dup=dropped_dup, avg_out_len=avg_len,
                dataset_hash=ds_hash,
                message=f"已导出 训练{len(train)}条 / 验证{len(val)}条"
                        f"（去重丢弃 {dropped_dup} 条）")
    _log(f"导出数据集 训练{len(train)} 验证{len(val)} 去重{dropped_dup} hash={ds_hash[:8]}")
    return {"samples": len(train), "val_samples": len(val),
            "dropped_dup": dropped_dup, "avg_out_len": avg_len,
            "jsonl": str(DATASET_JSONL), "val_jsonl": str(DATASET_VAL),
            "corpus": str(CORPUS_TXT), "dataset_hash": ds_hash}


# ---------- 训练 ----------
# ---------- 训练环境安装（pip 输出实时写入控制台日志） ----------
ENV_LOG = LAB / "env_install.log"
ENV_STATE = LAB / "env_install_state.json"
ENV_VENV = LAB.parent / "train_venv"
ENV_PY = ENV_VENV / "Scripts" / "python.exe"
ENV_SP = ENV_VENV / "Lib" / "site-packages"
ENV_PKGS = ["torch", "torchaudio", "transformers", "peft", "datasets",
            "accelerate", "safetensors", "soundfile"]
ENV_DEP_KEYS = ("torch", "transformers", "peft", "datasets", "accelerate", "soundfile")
_IPROC = {"p": None}


def _env_save(**kv):
    st = {}
    try:
        st = json.loads(ENV_STATE.read_text("utf-8"))
    except Exception:
        st = {}
    st.update(kv)
    ENV_STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        alive = bool(h)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
        return alive
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


_ENV_CACHE = {"ts": 0.0, "info": None}


def _env_probe_deps() -> dict:
    """用训练环境自己的解释器探测依赖（venv 可能继承系统 site-packages，
    只看 venv/Lib/site-packages 目录会误判缺包）。find_spec 不真正导入，很快。"""
    py = str(ENV_PY) if ENV_PY.exists() else sys.executable
    code = (
        "import importlib.util as u, json\n"
        "def has(m):\n"
        "    try:\n"
        "        return u.find_spec(m) is not None\n"
        "    except Exception:\n"
        "        return False\n"
        "print(json.dumps({m: has(m) for m in "
        + json.dumps(list(ENV_DEP_KEYS)) + "}))"
    )
    env = dict(os.environ)
    env.pop("PYTHONHASHSEED", None)
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, timeout=60,
                           env=env)
        if r.returncode == 0:
            return json.loads(r.stdout.decode("utf-8", "replace").strip() or "{}")
    except Exception:
        pass
    return {}


def env_check(force=False, ttl=30) -> dict:
    """检测训练环境：train_venv + 关键依赖是否齐全（结果缓存 ttl 秒）。"""
    if not force and _ENV_CACHE["info"] and time.time() - _ENV_CACHE["ts"] < ttl:
        return dict(_ENV_CACHE["info"])
    deps = _env_probe_deps()
    if not deps:   # 探测失败退回目录判断（快速但不看继承的系统包）
        deps = {m: (ENV_SP / m).is_dir() for m in ENV_DEP_KEYS}
    else:
        deps = {m: bool(deps.get(m)) for m in ENV_DEP_KEYS}
    ready = ENV_PY.exists() and deps["torch"] and deps["transformers"] and deps["peft"]
    info = {"venv": ENV_PY.exists(), "deps": deps, "ready": ready,
            "missing": [k for k, v in deps.items() if not v]}
    _ENV_CACHE.update(ts=time.time(), info=dict(info))
    return info


def env_probe() -> dict:
    """检测一次并把结果写进安装日志（页面控制台可见）。"""
    info = env_check(force=True)
    lines = [f"$ 检测训练环境  {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"  venv   : {ENV_VENV}  （{'存在' if info['venv'] else '缺失'}）"]
    for k, v in info["deps"].items():
        lines.append(f"  {k:<12}: {'已安装' if v else '未安装'}")
    lines.append("  结论   : " + ("环境就绪，可以开始训练" if info["ready"]
                                else "缺少 " + ("、".join(info["missing"]) or "训练栈")))
    try:
        with open(ENV_LOG, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass
    return info


def env_log(since=0, limit=262144) -> dict:
    """增量读取安装日志：返回 (data, next, size)，按整行截断避免中文乱码。"""
    if not ENV_LOG.exists():
        return {"data": "", "next": 0, "size": 0}
    size = ENV_LOG.stat().st_size
    try:
        since = max(0, min(int(since or 0), size))
    except (TypeError, ValueError):
        since = 0
    if since >= size:
        return {"data": "", "next": size, "size": size}
    with open(ENV_LOG, "rb") as f:
        f.seek(since)
        raw = f.read(limit)
    nxt = since + len(raw)
    if nxt < size:
        cut = raw.rfind(b"\n")
        if cut > 0:
            raw = raw[:cut + 1]
            nxt = since + len(raw)
    return {"data": raw.decode("utf-8", "replace"), "next": nxt, "size": size}


def env_status() -> dict:
    st = {}
    try:
        st = json.loads(ENV_STATE.read_text("utf-8"))
    except Exception:
        st = {}
    st.setdefault("status", "idle")
    if st.get("status") == "installing":
        p = _IPROC.get("p")
        alive = p is not None and p.poll() is None
        pid = st.get("pid") or 0
        if not alive and pid:
            alive = _pid_alive(pid)
        if not alive and not pid and time.time() - float(st.get("started") or 0) < 30:
            alive = True   # 刚启动还没拿到 pid
        if not alive:
            st.update(status="error",
                      message="安装进程已中断（服务重启或进程退出），请重新安装")
            _env_save(status="error", finished=time.time(), phase="中断",
                      message=st["message"])
    if st.get("started"):
        end = st.get("finished") or time.time()
        st["elapsed"] = max(0, int(end - float(st["started"])))
    st["env"] = env_check()
    # 自愈：记录是失败/中断，但依赖其实都在 → 直接判定安装完成
    if st["env"]["ready"] and st.get("status") == "error":
        st.update(status="done", phase="完成", message="训练环境已就绪")
        _env_save(status="done", phase="完成", message=st["message"])
    try:
        st["log_size"] = ENV_LOG.stat().st_size if ENV_LOG.exists() else 0
    except OSError:
        st["log_size"] = 0
    return st


def _env_stream(cmd, env) -> int:
    """运行一条命令，把 stdout+stderr 逐行写入安装日志（控制台所见即所得）。"""
    with open(ENV_LOG, "ab") as log:
        log.write(("\n$ " + " ".join(cmd) + "\n").encode("utf-8"))
        log.flush()
        p = subprocess.Popen(cmd, cwd=str(LAB), env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        _IPROC["p"] = p
        _env_save(pid=p.pid)
        try:
            for line in iter(p.stdout.readline, b""):
                log.write(line)
                log.flush()
        finally:
            try:
                p.stdout.close()
            except Exception:
                pass
        return p.wait()


def _env_worker(kind: str, pkgs) -> None:
    env = dict(os.environ)
    env.pop("PYTHONHASHSEED", None)
    env["PYTHONUNBUFFERED"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    try:
        # 1) 没有训练虚拟环境就先创建
        if not ENV_PY.exists():
            _env_save(phase="创建 venv", message="正在创建训练虚拟环境 train_venv…")
            rc = _env_stream([sys.executable, "-m", "venv", str(ENV_VENV)], env)
            if rc != 0:
                raise RuntimeError(f"创建 venv 失败（退出码 {rc}）")
        # 2) 组装安装命令
        cmds = []
        if kind == "gpu":
            for py in dict.fromkeys([str(ENV_PY), sys.executable]):
                if os.path.exists(py):
                    cmds.append([py, "-u", "-m", "pip", "install", "--upgrade",
                                 "--progress-bar", "off",
                                 "torch", "torchaudio", "--index-url",
                                 "https://download.pytorch.org/whl/cu121"])
        else:
            target = str(ENV_PY) if ENV_PY.exists() else sys.executable
            # 不加 --upgrade：已装好的包（如 CPU 版 torch）直接跳过，只补齐缺失依赖
            cmds.append([target, "-u", "-m", "pip", "install",
                         "--progress-bar", "off"] + list(pkgs or ENV_PKGS))
        total = len(cmds)
        for i, cmd in enumerate(cmds, 1):
            _env_save(phase=f"安装依赖 {i}/{total}",
                      message=f"正在安装（第 {i}/{total} 步），进度见控制台…")
            rc = _env_stream(cmd, env)
            if rc != 0:
                if i < total:
                    with open(ENV_LOG, "a", encoding="utf-8") as f:
                        f.write(f"[warn] 第 {i} 步退出码 {rc}，继续下一步\n")
                    continue
                raise RuntimeError(f"pip 安装失败（退出码 {rc}），详见控制台日志")
        info = env_check(force=True)
        ok = info["ready"] if kind != "gpu" else True
        msg = ("安装完成，训练环境已就绪" if ok
               else "命令执行完毕，但仍缺少：" + "、".join(info["missing"]))
        _env_save(status="done" if ok else "error", finished=time.time(),
                  phase="完成", message=msg)
        with open(ENV_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n[env] {msg}\n")
    except Exception as e:
        _env_save(status="error", finished=time.time(), phase="失败",
                  message=str(e))
        try:
            with open(ENV_LOG, "a", encoding="utf-8") as f:
                f.write(f"\n[env] 安装失败: {e}\n")
        except OSError:
            pass


def env_install(kind="cpu", pkgs=None) -> dict:
    """后台安装训练环境；输出流式写入安装日志，页面控制台实时显示。"""
    st = env_status()
    if st.get("status") == "installing":
        return {"ok": False, "message": "安装已在进行中，控制台实时显示进度"}
    try:
        ENV_LOG.write_text("", "utf-8")
    except OSError:
        pass
    _env_save(status="installing", kind=kind, pid=0, started=time.time(),
              finished=0, phase="准备中", message="正在准备安装…")
    threading.Thread(target=_env_worker, args=(kind, pkgs), daemon=True).start()
    return {"ok": True, "kind": kind,
            "message": "已开始安装，安装过程实时显示在控制台"}


def env_stop() -> dict:
    p = _IPROC.get("p")
    if p and p.poll() is None:
        try:
            p.kill()
        except Exception:
            pass
        _env_save(status="idle", finished=time.time(), phase="已停止",
                  message="已手动停止安装")
        return {"ok": True}
    return {"ok": False, "message": "没有正在进行的安装"}


def _find_engine():
    """返回 (engine_name, cmd_builder)；找不到返回 None 与安装指引"""
    venv_py = ENV_PY
    script = LAB / "qlora_train.py"
    info = env_check()
    if venv_py.exists() and script.exists() and info["ready"]:
        def build(adapter_out, rank, epochs, threads):
            cmd = [str(venv_py), str(script),
                   "--data", str(DATASET_JSONL),
                   "--out", str(adapter_out),
                   "--rank", str(rank), "--epochs", str(epochs),
                   "--patience", "2"]
            if DATASET_VAL.exists():
                cmd += ["--val", str(DATASET_VAL)]
            return cmd
        return "peft", build
    lack = "、".join(info["missing"]) or "train_venv"
    return None, (f"训练环境未就绪（缺少 {lack}）。请在训练中心点「📦 安装训练环境」，"
                  "安装过程会在下方控制台实时显示。")


def train_start(lora_rank=8, epochs=3, threads=0, base="") -> dict:
    st = status()
    if st.get("status") == "training":
        return {"ok": False, "message": "训练已在进行中"}
    if not DATASET_JSONL.exists() or st.get("samples", 0) == 0:
        return {"ok": False, "message": "请先调用 /lora/export 导出训练集"}
    engine, builder = _find_engine()
    if not engine:
        _save_state(status="error", message=builder)
        return {"ok": False, "message": builder}
    adapter_out = LAB / f"adapter_{time.strftime('%m%d_%H%M')}"
    cmd = builder(adapter_out, lora_rank, epochs, threads)
    if base:
        cmd += ["--base", base]
    TRAIN_LOG.write_text("", "utf-8")
    _log("启动训练: " + " ".join(cmd))
    # 防御：PYTHONHASHSEED 非法值会导致子进程解释器直接崩溃
    # （合法值仅 "random" 或 [0, 4294967295] 整数）
    env = dict(os.environ)
    hs = env.get("PYTHONHASHSEED")
    if hs is not None and hs != "random":
        try:
            v = int(hs)
            if not (0 <= v <= 4294967295):
                env.pop("PYTHONHASHSEED", None)
        except (TypeError, ValueError):
            env.pop("PYTHONHASHSEED", None)
    try:
        p = subprocess.Popen(cmd, cwd=str(LAB), env=env,
                             stdout=open(TRAIN_LOG, "a", encoding="utf-8"),
                             stderr=subprocess.STDOUT)
    except Exception as e:
        _save_state(status="error", message=f"启动失败: {e}")
        return {"ok": False, "message": str(e)}
    _PROC["p"] = p
    ds_hash = _md5_file(DATASET_JSONL)
    started = time.time()
    _save_state(status="training", engine=engine, pid=p.pid,
                adapter=str(adapter_out), started=started, dataset_hash=ds_hash,
                message=f"训练启动 (rank={lora_rank}, epochs={epochs}, engine={engine})")

    def _watch():
        rc = p.wait()
        ok = rc == 0 and (adapter_out / "adapter_config.json").exists()
        # 从日志汇总训练指标，写入历史记录
        steps = tokens = 0
        final_loss = None
        best_eval = None
        best_epoch = 0
        eval_curve = []
        for ln in tail_log(4000):
            m = re.search(r"\[train\] step (\d+)/(\d+) loss=([\d.]+) tokens=(\d+)", ln)
            if m:
                steps, tokens = int(m.group(2)), int(m.group(4))
                final_loss = float(m.group(3))
            m = re.search(r"\[train\] epoch (\d+) train_loss=([\d.]+) eval_loss=([\d.]+)", ln)
            if m:
                eval_curve.append({"epoch": int(m.group(1)),
                                   "train": float(m.group(2)),
                                   "eval": float(m.group(3))})
            m = re.search(r"\[train\] DONE tokens=(\d+) best_epoch=(\d+) best_eval=(-?[\d.]+)", ln)
            if m:
                tokens = int(m.group(1)); best_epoch = int(m.group(2))
                best_eval = float(m.group(3))
        fin = time.time()
        if ok:
            msg = (f"训练完成，adapter: {adapter_out.name}。"
                   f"（PEFT 格式；挂载 7B GGUF 需先用 convert_lora_to_gguf.py 转换）")
            _save_state(status="done", finished=fin, message=msg)
        else:
            msg = f"训练进程退出码 {rc}，查看 train.log"
            _save_state(status="error", finished=fin, message=msg)
        _hist_add({"type": "persona", "adapter": adapter_out.name,
                   "started": started, "finished": fin,
                   "samples": status().get("samples", 0),
                   "steps": steps, "final_loss": final_loss, "tokens": tokens,
                   "best_eval": best_eval, "best_epoch": best_epoch,
                   "eval_curve": eval_curve,
                   "dataset_hash": ds_hash,
                   "status": "done" if ok else "error", "message": msg})
        _log(f"训练结束 rc={rc} tokens={tokens} best_eval={best_eval}")
    threading.Thread(target=_watch, daemon=True).start()
    return {"ok": True, "adapter": adapter_out.name}


def train_stop() -> dict:
    p = _PROC.get("p")
    if p and p.poll() is None:
        p.kill()
        _save_state(status="idle", message="已手动停止")
        return {"ok": True}
    return {"ok": False, "message": "无运行中的训练"}


def apply(adapter_name: str) -> dict:
    """挂载 GGUF adapter 到配置，重启后生效（走 self_restart 四层闸门）"""
    path = LAB / adapter_name
    if not path.exists():
        return {"ok": False, "message": f"adapter 不存在: {adapter_name}"}
    if path.is_dir() and (path / "adapter_config.json").exists():
        return {"ok": False,
                "message": "这是 PEFT 格式 adapter，需先用 llama.cpp 的 "
                           "convert_lora_to_gguf.py 转成 .gguf 后再挂载"}
    import config as _cfg
    _cfg.set_value("lora.path", str(path))
    _save_state(message=f"已挂载 {adapter_name}，重启服务后生效")
    return {"ok": True, "message": "已写入 config lora.path，调用 self_restart 重启后生效",
            "path": str(path)}


# ---------- 训练前置校验（重复资料拦截） ----------
def precheck(kind: str) -> dict:
    """训练前校验资料是否已用于历史训练。返回 {duplicate, hash, last}"""
    if kind == "voice":
        h, md5s, n = _voice_set_hash()
        if not md5s:
            return {"ok": False, "duplicate": False, "hash": "",
                    "message": "没有可用的声音样本，请先在上方上传"}
        for rec in reversed(history()):
            if rec.get("type") == "voice" and rec.get("status") == "done" \
                    and rec.get("dataset_hash") == h:
                return {"ok": True, "duplicate": True, "hash": h, "samples": n,
                        "last": rec,
                        "message": f"当前 {n} 段样本与 "
                                   f"{time.strftime('%m-%d %H:%M', time.localtime(rec.get('started', 0)))} "
                                   f"的训练资料完全相同"}
        return {"ok": True, "duplicate": False, "hash": h, "samples": n}
    # persona：以最近一次导出的 dataset.jsonl 指纹为准
    if not DATASET_JSONL.exists():
        return {"ok": False, "duplicate": False, "hash": "",
                "message": "尚未导出训练集"}
    h = _md5_file(DATASET_JSONL)
    for rec in reversed(history()):
        if rec.get("type") == "persona" and rec.get("status") == "done" \
                and rec.get("dataset_hash") == h:
            return {"ok": True, "duplicate": True, "hash": h, "last": rec,
                    "message": "当前训练集与 "
                               f"{time.strftime('%m-%d %H:%M', time.localtime(rec.get('started', 0)))} "
                               f"训练过的资料完全相同（adapter: {rec.get('adapter', '')}）"}
    return {"ok": True, "duplicate": False, "hash": h}


# ---------- 声音样本质检（时长/采样率/削波/静音比 → 清洗参考音） ----------
def _wav_qc(fp):
    """读取音频并给出质检指标；失败返回 None。"""
    try:
        import numpy as np
        import soundfile as sf
        d, sr = sf.read(str(fp), dtype="float32", always_2d=True)
        mono = d.mean(axis=1)
        if not len(mono):
            return None
        dur = len(mono) / float(sr or 1)
        peak = float(np.abs(mono).max() or 0.0)
        clip = float((np.abs(mono) > 0.985).mean())
        frame = max(1, int(sr * 0.01))
        n = len(mono) // frame
        if n:
            e = np.sqrt((mono[:n * frame].reshape(n, frame) ** 2).mean(axis=1))
            thr = max(1e-4, float(e.max()) * 0.02)
            silence = float((e < thr).mean())
        else:
            silence = 1.0
        rms = float(np.sqrt((mono ** 2).mean()) or 0.0)
        return {"dur": dur, "sr": int(sr), "peak": peak, "clip": clip,
                "silence": silence, "rms": rms,
                "_data": mono, "_sr": int(sr)}
    except Exception:
        return None


def _clean_ref(qc, max_sec=12.0):
    """裁掉首尾静音 + 峰值归一化，返回 (mono, sr)"""
    import numpy as np
    mono, sr = qc["_data"], qc["_sr"]
    frame = max(1, int(sr * 0.01))
    n = max(1, len(mono) // frame)
    e = np.sqrt((mono[:n * frame].reshape(n, frame) ** 2).mean(axis=1))
    thr = max(1e-4, float(e.max()) * 0.03)
    idx = np.where(e >= thr)[0]
    if len(idx):
        a = max(0, int(idx[0] * frame - sr * 0.12))
        b = min(len(mono), int((idx[-1] + 1) * frame + sr * 0.18))
        mono = mono[a:b]
    # 参考音控制在 12s 内（过长会让克隆音色漂移）
    if len(mono) > int(max_sec * sr):
        mid = len(mono) // 2
        half = int(max_sec * sr / 2)
        mono = mono[max(0, mid - half): mid + half]
    peak = float(np.abs(mono).max() or 0.0)
    if peak > 1e-6:
        mono = mono * (0.89 / peak)
    return mono, sr


# ---------- 声音训练（素材校准链路） ----------
def voice_train(force=False) -> dict:
    """针对当前声音克隆模型（F5-TTS）的微调优化。
    本机 CPU 无法对 F5 大模型做权重级微调，这里执行等效校准流程：
    样本质检（时长/削波/静音比）→ 去重拦截 → 遴选最优参考音
    → 裁静音+归一化重建参考音 → 重建克隆缓存。"""
    exts = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    cands = []
    for f in sorted(os.listdir(VOICE_DIR)):
        if f == "ref_speech_10s.wav" or Path(f).suffix.lower() not in exts:
            continue
        fp = VOICE_DIR / f
        qc = _wav_qc(fp)
        dur = qc["dur"] if qc else 0.0
        cands.append({"name": f, "path": fp, "size": fp.stat().st_size,
                      "dur": dur, "qc": qc})
    if not cands:
        return {"ok": False, "message": "没有可用的声音样本，请先上传"}
    pc = precheck("voice")
    if pc.get("duplicate") and not force:
        return {"ok": False, "duplicate": True,
                "message": pc.get("message", "训练资料与历史重复") +
                           "。如确认要再次训练，请勾选「强制重复训练」"}

    # 质检：太短(<3s)/过长(>60s)/削波严重/几乎全静音的样本不参与参考音评选
    report, usable = [], []
    for c in cands:
        q = c["qc"]
        if not q:
            report.append({"name": c["name"], "dur": round(c["dur"], 1),
                           "ok": False, "why": "无法解码"})
            continue
        why = []
        if q["dur"] < 3:
            why.append("过短")
        if q["dur"] > 60:
            why.append("过长")
        if q["clip"] > 0.01:
            why.append(f"削波{q['clip']*100:.1f}%")
        if q["silence"] > 0.7:
            why.append(f"静音{q['silence']*100:.0f}%")
        ok = not why
        report.append({"name": c["name"], "dur": round(q["dur"], 1),
                       "sr": q["sr"], "peak": round(q["peak"], 3),
                       "silence_pct": round(q["silence"] * 100, 1),
                       "clip_pct": round(q["clip"] * 100, 2),
                       "ok": ok, "why": "、".join(why)})
        if ok:
            usable.append(c)

    # 遴选最优参考音：优先 6~40s，静音少、无削波
    def _score(c):
        q = c["qc"]
        mid = 1.0 if 6 <= q["dur"] <= 40 else 0.0
        return (mid, -q["silence"], -q["clip"], -abs(q["dur"] - 15))
    pool = usable or [c for c in cands if c["qc"]]
    if not pool:
        return {"ok": False, "message": "所有样本都无法解码，请上传 wav/mp3 试音",
                "report": report}
    best = max(pool, key=_score)
    ref = VOICE_DIR / "ref_speech_10s.wav"
    try:
        mono, sr = _clean_ref(best["qc"])
        import soundfile as sf
        sf.write(str(ref), mono, sr, subtype="PCM_16")
        txt = ref.with_suffix(".txt")
        if txt.exists():
            txt.unlink()  # 清转写缓存，下次克隆重新转写
        import config as _cfg
        _cfg.load()
        _cfg.set_value("voice.clone_ref", "ref_speech_10s.wav")
        # 训练产出 = 语音包（音色卡）：自动登记最优样本为一张卡，视频通话直接选用
        profiles = [p for p in (_cfg.get("voices.profiles") or [])
                    if not str(p.get("name", "")).startswith("晓欣语音包")]
        profiles.append({"name": "晓欣语音包·" + time.strftime("%m%d-%H%M"),
                         "sample": best["name"],
                         "created": time.strftime("%Y-%m-%d %H:%M:%S")})
        _cfg.set_value("voices.profiles", profiles)
    except Exception as e:
        return {"ok": False, "message": f"参考音校准失败: {e}", "report": report}
    fin = time.time()
    msg = (f"声音校准完成：已选 {best['name']}"
           + (f"（{best['qc']['dur']:.1f}s）" if best["qc"] else "")
           + f" 作为克隆参考，共校验 {len(cands)} 段样本，"
             f"{len(usable)} 段通过质检。已自动裁剪首尾静音并归一化音量。"
             "已生成语音包（音色卡），视频通话页顶栏会自动选中它。"
             "本机无 GPU，F5-TTS 权重级微调未执行；已更新参考音并重建克隆缓存。")
    _hist_add({"type": "voice", "adapter": "f5-tts-clone-ref",
               "started": fin, "finished": fin, "samples": len(cands),
               "steps": 0, "final_loss": None, "tokens": 0,
               "dataset_hash": pc.get("hash", ""),
               "files": [c["name"] for c in cands],
               "report": report,
               "status": "done", "message": msg})
    _log(f"声音校准: 参考音={best['name']} 样本={len(cands)} 合格={len(usable)}")
    return {"ok": True, "message": msg, "ref": best["name"],
            "samples": len(cands), "usable": len(usable), "report": report}


# ================================================================
# 完善训练系统（四期）：数据管理 / 可视化 / 对比 / 反馈学习 / 知识注入 / 自动训练
# ================================================================


# ---------- 1. 训练数据管理：浏览 / 编辑 / 删除 / 质量评分 ----------
def _load_deleted() -> set:
    if DELETED_PATH.exists():
        try:
            return set(json.loads(DELETED_PATH.read_text("utf-8")))
        except Exception:
            pass
    return set()


def _save_deleted(s: set):
    DELETED_PATH.write_text(json.dumps(list(s)), "utf-8")


def _sample_score(s: dict) -> dict:
    """给单条训练样本打质量分（0-100），并给出建议。"""
    q, a = s.get("instruction", ""), s.get("output", "")
    score = 50
    flags = []
    # 长度
    if len(a) < 30:
        score -= 25; flags.append("回答过短")
    elif len(a) > 1200:
        score -= 8; flags.append("回答过长")
    else:
        score += 12
    if len(q) < 4:
        score -= 15; flags.append("问题过短")
    elif len(q) > 200:
        score += 5
    # 多样性
    if len(set(a)) < 15:
        score -= 20; flags.append("内容单调")
    # 工具调用成功的样本加分
    if s.get("tool"):
        score += 10
    # 含表情符号的样本（TTS 不友好）轻微扣分
    if re.search(r"[\U0001F000-\U0001FFFF]", a):
        score -= 5; flags.append("含emoji")
    # 含 markdown 代码块的样本扣分（训练人设不需要）
    if "```" in a:
        score -= 8; flags.append("含代码块")
    score = max(0, min(100, score))
    return {"score": score, "flags": flags}


def dataset_list() -> dict:
    """列出当前训练集所有样本（含质量分），供训练中心浏览。"""
    rows = []
    if DATASET_JSONL.exists():
        for ln in DATASET_JSONL.read_text("utf-8").splitlines():
            try:
                s = json.loads(ln)
                rows.append(s)
            except Exception:
                pass
    deleted = _load_deleted()
    out = []
    for i, s in enumerate(rows):
        key = hashlib.md5((s.get("instruction", "") + "\u0001" +
                           s.get("output", "")).encode("utf-8")).hexdigest()
        sc = _sample_score(s)
        out.append({
            "idx": i, "key": key,
            "instruction": s.get("instruction", ""),
            "output": s.get("output", ""),
            "tool": bool(s.get("tool")),
            "score": sc["score"], "flags": sc["flags"],
            "deleted": key in deleted
        })
    return {"samples": out, "total": len(out),
            "avg_score": round(sum(x["score"] for x in out) / max(1, len(out)), 1)}


def dataset_edit(idx: int, instruction: str, output: str) -> dict:
    """编辑某条训练样本的问答内容。"""
    rows = []
    if DATASET_JSONL.exists():
        for ln in DATASET_JSONL.read_text("utf-8").splitlines():
            try:
                rows.append(json.loads(ln))
            except Exception:
                rows.append(None)
    if idx < 0 or idx >= len(rows) or not rows[idx]:
        return {"ok": False, "error": "样本下标越界"}
    rows[idx]["instruction"] = instruction[:800]
    rows[idx]["output"] = output[:1500]
    with open(DATASET_JSONL, "w", encoding="utf-8") as f:
        for r in rows:
            if r:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    _save_state(dataset_hash=_md5_file(DATASET_JSONL))
    return {"ok": True, "idx": idx}


def dataset_delete(idx: int) -> dict:
    """标记某条样本为已删除（不真正删文件，导出时跳过）。"""
    rows = []
    if DATASET_JSONL.exists():
        for ln in DATASET_JSONL.read_text("utf-8").splitlines():
            try:
                rows.append(json.loads(ln))
            except Exception:
                rows.append(None)
    if idx < 0 or idx >= len(rows) or not rows[idx]:
        return {"ok": False, "error": "样本下标越界"}
    key = hashlib.md5((rows[idx].get("instruction", "") + "\u0001" +
                       rows[idx].get("output", "")).encode("utf-8")).hexdigest()
    deleted = _load_deleted()
    deleted.add(key)
    _save_deleted(deleted)
    return {"ok": True, "idx": idx, "remaining": len(rows) - len(deleted)}


def voice_qc_list() -> dict:
    """列出所有声音样本及质检结果，供训练中心展示。"""
    exts = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}
    items = []
    if VOICE_DIR.exists():
        for f in sorted(os.listdir(VOICE_DIR)):
            if f == "ref_speech_10s.wav" or Path(f).suffix.lower() not in exts:
                continue
            fp = VOICE_DIR / f
            qc = _wav_qc(fp)
            if qc:
                items.append({
                    "name": f, "size": fp.stat().st_size,
                    "dur": round(qc["dur"], 1), "sr": qc["sr"],
                    "peak": round(qc["peak"], 3),
                    "silence_pct": round(qc["silence"] * 100, 1),
                    "clip_pct": round(qc["clip"] * 100, 2),
                    "rms": round(qc["rms"], 4),
                    "ok": 3 <= qc["dur"] <= 60 and qc["clip"] <= 0.01 and qc["silence"] <= 0.7
                })
            else:
                items.append({"name": f, "size": fp.stat().st_size,
                              "dur": 0, "ok": False, "why": "无法解码"})
    return {"samples": items, "total": len(items),
            "usable": sum(1 for x in items if x.get("ok"))}


# ---------- 2. 训练效果对比：训练前 vs 训练后 ----------
def compare_responses(question: str) -> dict:
    """用同一问题分别询问基础模型和训练后模型，返回两份回答。"""
    import config as _cfg
    _cfg.load()
    base_resp = ""
    trained_resp = ""
    # 基础模型：临时卸载 adapter
    try:
        base_resp = _ask_llm(question, use_adapter=False)
    except Exception as e:
        base_resp = f"[基础模型调用失败: {e}]"
    # 训练后模型：使用当前 adapter
    try:
        trained_resp = _ask_llm(question, use_adapter=True)
    except Exception as e:
        trained_resp = f"[训练模型调用失败: {e}]"
    # 简易打分：回答长度、是否含用户称呼、是否自然
    def _sc(text):
        s = 40
        if 30 < len(text) < 1000: s += 20
        if "晓欣" in text or "我是" in text: s += 10
        if "```" not in text: s += 10
        if not re.search(r"[\U0001F000-\U0001FFFF]", text): s += 10
        return min(100, s)
    return {
        "question": question,
        "base": base_resp, "base_score": _sc(base_resp),
        "trained": trained_resp, "trained_score": _sc(trained_resp)
    }


def _ask_llm(question: str, use_adapter: bool) -> str:
    """调用本地 LLM。use_adapter=False 时临时禁用 LoRA。
    注意：在 api_server 进程内复用共享模型时，适配器在启动时已挂载、无法按调用卸载，
    此时"基础模型"回答会带适配器影响并加注说明；独立子进程模式下禁用真实生效。"""
    import config as _cfg
    _cfg.load()
    saved = _cfg.get("lora.path")
    if not use_adapter:
        _cfg.set_value("lora.path", "")
    try:
        from chat_engine import ChatEngine
        engine = ChatEngine()
        msg = engine.chat(question, session_id="compare_test")
        reply = msg.get("reply", "") if isinstance(msg, dict) else str(msg)
        if not use_adapter and getattr(engine, "shared", False) and saved:
            reply += ("\n（注：服务进程已挂载适配器，无法临时卸载，此回答仍受适配器影响；"
                      "想做纯基础模型对比，请先 /lora/apply 卸载并重启）")
        return reply
    finally:
        if not use_adapter and saved:
            _cfg.set_value("lora.path", saved)


# ---------- 3. 对话纠错学习：点赞/点踩 → 训练集 ----------
def feedback_add(question: str, original: str, corrected: str, vote: str) -> dict:
    """记录用户反馈。vote='up' 点赞入正样本，'down' 用 corrected 替换原回答。"""
    rec = {
        "time": time.time(),
        "question": question[:800],
        "original": original[:1500],
        "corrected": corrected[:1500] if corrected else "",
        "vote": vote
    }
    with open(FEEDBACK_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    # 点赞且有回答 → 直接作为高质量样本入训练集
    if vote == "up" and original:
        with open(DATASET_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps({"instruction": question[:800], "input": "",
                                "output": original[:1500], "tool": False,
                                "source": "feedback_up"}, ensure_ascii=False) + "\n")
        _save_state(dataset_hash=_md5_file(DATASET_JSONL))
    # 点踩且有修正 → 修正版入训练集
    if vote == "down" and corrected:
        with open(DATASET_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps({"instruction": question[:800], "input": "",
                                "output": corrected[:1500], "tool": False,
                                "source": "feedback_down"}, ensure_ascii=False) + "\n")
        _save_state(dataset_hash=_md5_file(DATASET_JSONL))
    _log(f"反馈{vote}: Q={question[:30]}")
    return {"ok": True, "vote": vote}


def feedback_list() -> dict:
    rows = []
    if FEEDBACK_PATH.exists():
        for ln in FEEDBACK_PATH.read_text("utf-8").splitlines():
            try:
                rows.append(json.loads(ln))
            except Exception:
                pass
    rows.reverse()
    return {"items": rows[:100], "total": len(rows),
            "up": sum(1 for r in rows if r.get("vote") == "up"),
            "down": sum(1 for r in rows if r.get("vote") == "down")}


# ---------- 4. 知识注入：上传文档 → 提取 → 训练集 ----------
def knowledge_add(text: str, source: str = "") -> dict:
    """把一段知识文本切成问答对加入知识池，训练时并入数据集。"""
    text = text.strip()
    if len(text) < 10:
        return {"ok": False, "message": "知识内容过短"}
    rec = {"time": time.time(), "source": source or "手动输入",
           "text": text[:5000]}
    with open(KNOWLEDGE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    # 自动生成一条 Q&A 样本：问"介绍一下xxx"，答为知识摘要
    instr = f"介绍一下关于{source[:20] if source else '这方面'}的情况"
    with open(DATASET_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps({"instruction": instr, "input": "",
                            "output": text[:1200], "tool": False,
                            "source": "knowledge"}, ensure_ascii=False) + "\n")
    _save_state(dataset_hash=_md5_file(DATASET_JSONL))
    _log(f"知识注入: source={source[:30]} len={len(text)}")
    return {"ok": True, "source": source, "len": len(text)}


def knowledge_list() -> dict:
    rows = []
    if KNOWLEDGE_PATH.exists():
        for ln in KNOWLEDGE_PATH.read_text("utf-8").splitlines():
            try:
                rows.append(json.loads(ln))
            except Exception:
                pass
    rows.reverse()
    return {"items": rows[:100], "total": len(rows)}


# ---------- 5. 自动定期训练：阈值检查 + 触发 ----------
def autotrain_cfg() -> dict:
    if AUTOTRAIN_CFG.exists():
        try:
            return json.loads(AUTOTRAIN_CFG.read_text("utf-8"))
        except Exception:
            pass
    return {"enabled": False, "threshold": 50, "last_train": 0}


def autotrain_set(enabled: bool, threshold: int = 50) -> dict:
    cfg = autotrain_cfg()
    cfg["enabled"] = bool(enabled)
    cfg["threshold"] = max(10, int(threshold))
    AUTOTRAIN_CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), "utf-8")
    return {"ok": True, "cfg": cfg}


def autotrain_check() -> dict:
    """检查是否需要自动训练：新对话数超过阈值。"""
    cfg = autotrain_cfg()
    if not cfg.get("enabled"):
        return {"need": False, "reason": "自动训练未开启"}
    last = cfg.get("last_train", 0)
    hist = EncLines(str(_DIR / "history.enc")).load_all()
    new_chats = [r for r in hist if r.get("type") == "chat"
                 and r.get("time_ts", 0) > last] if last else hist
    # 如果 history 没有 time_ts 字段，按总数估算
    if not new_chats and hist:
        new_chats = hist[:cfg["threshold"] + 1]
    n = len(new_chats)
    if n >= cfg["threshold"]:
        return {"need": True, "new_chats": n, "threshold": cfg["threshold"]}
    return {"need": False, "new_chats": n, "threshold": cfg["threshold"]}


def autotrain_mark_done():
    cfg = autotrain_cfg()
    cfg["last_train"] = time.time()
    AUTOTRAIN_CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), "utf-8")


# 在 export_dataset 中合并反馈/知识样本（已直接写入 dataset.jsonl，无需额外处理）
# 这里提供一个汇总统计供 UI 展示
def training_overview() -> dict:
    ds = dataset_list()
    fb = feedback_list()
    kb = knowledge_list()
    vq = voice_qc_list()
    auto = autotrain_cfg()
    last_train = max((r.get("started", 0) for r in history() if r.get("status") == "done"),
                     default=0)
    # 微信学习样本数
    wechat_n = 0
    wechat_ds = _DIR / "training_data" / "wechat_learn.jsonl"
    if wechat_ds.exists():
        try:
            wechat_n = sum(1 for l in wechat_ds.read_text("utf-8").splitlines() if l.strip())
        except Exception:
            pass
    return {
        "dataset_total": ds["total"],
        "dataset_avg_score": ds["avg_score"],
        "feedback_total": fb["total"],
        "feedback_up": fb["up"],
        "feedback_down": fb["down"],
        "knowledge_total": kb["total"],
        "wechat_total": wechat_n,
        "voice_total": vq["total"],
        "voice_usable": vq["usable"],
        "autotrain": auto,
        "last_train": last_train
    }

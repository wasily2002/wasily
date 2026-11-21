# -*- coding: utf-8 -*-
"""
wasily 集群 worker（单文件 · 仅标准库）—— 手机/电脑都能跑
用法:
  python worker.py --server http://主节点地址:8000 --token 接入令牌 [--name 我的名字]
手机(Termux): pkg install python && curl -O http://主节点:8000/dist/worker.py && python worker.py ...
能力自举（可选，装了就能领重活）:
  pip install faster-whisper   → 可领 whisper 转写任务
  pip install rembg onnxruntime pillow → 可领 cutout 抠图任务
互联网接入: 主节点用花生壳映射 8000 端口，--server 填花生壳域名即可。
"""
import argparse
import base64
import io
import json
import os
import platform
import sys
import tempfile
import time
import urllib.request


def detect_caps():
    caps = []
    try:
        import faster_whisper  # noqa
        caps.append("whisper")
    except ImportError:
        pass
    try:
        import rembg, onnxruntime  # noqa
        caps.append("cutout")
    except ImportError:
        pass
    return caps


_WHISPER = None


def do_whisper(payload):
    global _WHISPER
    audio = base64.b64decode(payload["audio_b64"])
    tmp = os.path.join(tempfile.gettempdir(), f"wxy_{int(time.time()*1000)}.wav")
    with open(tmp, "wb") as f:
        f.write(audio)
    try:
        if _WHISPER is None:
            from faster_whisper import WhisperModel
            _WHISPER = WhisperModel(payload.get("model", "small"), device="cpu",
                                    compute_type="int8")
        segs, _ = _WHISPER.transcribe(tmp, language="zh", beam_size=3)
        return {"text": "".join(s.text for s in segs).strip()}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def do_cutout(payload):
    from rembg import remove, new_session
    img = base64.b64decode(payload["image_b64"])
    sess = new_session("u2net_human_seg")
    out = remove(img, session=sess, post_process_mask=True)
    return {"image_b64": base64.b64encode(out).decode()}


def do_sysinfo(_payload):
    return {"platform": platform.platform(), "python": platform.python_version(),
            "machine": platform.machine(), "cpu_count": os.cpu_count()}


def run_task(t):
    p = t.get("payload") or {}
    if t["type"] == "whisper":
        return do_whisper(p)
    if t["type"] == "cutout":
        return do_cutout(p)
    if t["type"] == "sysinfo":
        return do_sysinfo(p)
    if t["type"] == "echo":
        return {"echo": p}
    raise ValueError("未知任务类型 " + t["type"])


def post(server, path, data, timeout=40):
    req = urllib.request.Request(server.rstrip("/") + path,
                                 data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--name", default="")
    args = ap.parse_args()

    caps = detect_caps()
    name = args.name or platform.node() or "worker"
    print(f"[worker] 能力: {caps or ['(仅基础)']}  主节点: {args.server}", flush=True)

    r = post(args.server, "/dist/register", {
        "token": args.token, "name": name, "caps": caps,
        "platform": platform.platform()})
    if not r.get("ok"):
        print("[worker] 注册失败:", r.get("error")); sys.exit(1)
    wid = r["worker_id"]
    print(f"[worker] 已接入 id={wid}，开始轮询任务…", flush=True)

    hb = 0
    while True:
        try:
            hb += 1
            if hb % 10 == 0:
                post(args.server, "/dist/heartbeat",
                     {"token": args.token, "worker_id": wid})
            r = post(args.server, "/dist/claim",
                     {"token": args.token, "worker_id": wid, "caps": caps},
                     timeout=40)
            task = r.get("task")
            if not task:
                continue
            print(f"[worker] 领到任务 {task['id']} ({task['type']})", flush=True)
            try:
                res = run_task(task)
                ok = True
            except Exception as e:
                res = {"error": f"{type(e).__name__}: {e}"}
                ok = False
            post(args.server, "/dist/result",
                 {"token": args.token, "worker_id": wid,
                  "task_id": task["id"], "ok": ok, "result": res})
            print(f"[worker] 任务 {task['id']} {'完成' if ok else '失败'}", flush=True)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[worker] 网络异常，5s 后重试: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()

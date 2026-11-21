# -*- coding: utf-8 -*-
"""临时：真实委派一次 reviewer（跑完即删）"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8000"


def post(path, payload, timeout):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))


def get(path, timeout=10):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=timeout).read().decode("utf-8"))


st = get("/agent_status")
print("PRE_STATUS:", st.get("state"), "| sid:", st.get("sid"), flush=True)

prompt = ("请用 reviewer 子智能体审查 wasily/AI/subagents.py 的代码质量与风险。"
          "必须调用 agent_delegate 委派给 reviewer，不要自己读文件。")
t0 = time.time()
r = post("/v1/chat/completions", {
    "messages": [{"role": "user", "content": prompt}],
    "session_id": "vscodium",
    "mode": "solo",
    "max_tool_rounds": 12,
    "max_think_seconds": 420,
}, 900)
el = time.time() - t0
print("ELAPSED: %.1fs" % el, flush=True)

txt = ""
try:
    txt = r["choices"][0]["message"]["content"]
except Exception:
    txt = json.dumps(r, ensure_ascii=False)[:800]
print("=== 回复 ===")
print(txt[:1500], flush=True)

evs = get("/tool_events?since=0").get("events") or []
subs = [e for e in evs if e.get("agent")]
print("=== 子智能体事件 ===")
print([(e.get("type"), e.get("agent"), e.get("ok"), e.get("tools"), e.get("dur")) for e in subs], flush=True)
print("=== 主循环工具事件（末 8 条） ===")
main = [(e.get("type"), e.get("tool")) for e in evs if not e.get("agent")]
print(main[-8:], flush=True)

# -*- coding: utf-8 -*-
"""
把 AIGCPanel（或其他数字人工具）离线渲染出的真人口型视频一键接入视频通话。

用法：
  1. 在 AIGCPanel 里用本人照片 + 台词生成带口型的 mp4
  2. 文件按状态命名后放进 avatars/render/，例如：
       idle_01.mp4   待机/倾听
       talk_01.mp4   说话
       walk_01.mp4   走动
       sit_01.mp4    坐着
       greet_01.mp4  打招呼
       happy_01.mp4  开心
       react_01.mp4  情绪反应
       sad_01.mp4    难过
  3. 运行：python import_render.py
     - 视频会被复制到 avatars/ 并自动更新 avatars/actions.json
     - 每个状态若有多条，按文件名排序取第一条
"""
import json
import os
import shutil

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "avatars", "render")
DST = os.path.join(BASE, "avatars")
ACT_FP = os.path.join(DST, "actions.json")

STATES = ("idle", "talk", "walk", "sit", "greet", "happy", "react", "sad")


def main():
    os.makedirs(SRC, exist_ok=True)
    try:
        with open(ACT_FP, encoding="utf-8") as f:
            acts = json.load(f)
    except Exception:
        acts = {}
    wxx = acts.setdefault("wxx", {})

    found = {}
    for fn in sorted(os.listdir(SRC)):
        if not fn.lower().endswith((".mp4", ".webm", ".mov")):
            continue
        key = fn.split("_")[0].lower()
        if key not in STATES:
            print("跳过（文件名前缀不是状态名）:", fn)
            continue
        found.setdefault(key, fn)

    if not found:
        print("avatars/render/ 里没有可导入的视频。")
        print("请把渲染好的 mp4 命名为 idle_01.mp4 / talk_01.mp4 / walk_01.mp4 ... 放进去。")
        return

    for key, fn in found.items():
        shutil.copy2(os.path.join(SRC, fn), os.path.join(DST, "render_" + fn))
        wxx[key] = "avatars/render_" + fn
        print("接入 %-6s <- %s" % (key, fn))

    with open(ACT_FP, "w", encoding="utf-8") as f:
        json.dump(acts, f, ensure_ascii=False, indent=1)
    print("\n已更新", ACT_FP)
    print("刷新视频通话页即可生效（形象选「视频数字人」/ video 模式）。")


if __name__ == "__main__":
    main()

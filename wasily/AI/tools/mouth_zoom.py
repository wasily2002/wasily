# -*- coding: utf-8 -*-
"""把几帧的嘴部区域放大拼成一张图，方便肉眼检查口型幅度。"""
import sys
import cv2
import numpy as np

src = sys.argv[1]
out = sys.argv[2]
times = [float(t) for t in sys.argv[3].split(",")]

cap = cv2.VideoCapture(src)
n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
tiles = []
for t in times:
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ok, fr = cap.read()
    if not ok:
        continue
    h, w = fr.shape[:2]
    # 嘴部大致位置：画面中部偏下
    y1, y2 = int(h * 0.42), int(h * 0.62)
    x1, x2 = int(w * 0.22), int(w * 0.72)
    m = fr[y1:y2, x1:x2]
    m = cv2.resize(m, (420, 260), interpolation=cv2.INTER_NEAREST)
    cv2.putText(m, "%.1fs" % t, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
    tiles.append(m)
cap.release()
if tiles:
    cv2.imwrite(out, np.hstack(tiles))
    print("saved", out, len(tiles))

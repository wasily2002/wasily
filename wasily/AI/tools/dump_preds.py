# -*- coding: utf-8 -*-
"""诊断：导出一段音频对应的 96x96 生成脸，拼成图看口型幅度。"""
import os
import sys
import tempfile
import shutil
import subprocess

import cv2
import numpy as np
import soundfile as sf
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lip_sync as L          # noqa: E402

face = sys.argv[1]
audio = sys.argv[2]
out = sys.argv[3]
start_f = int(sys.argv[4]) if len(sys.argv) > 4 else 20
count = int(sys.argv[5]) if len(sys.argv) > 5 else 12

tmp = tempfile.mkdtemp()
try:
    wav16 = os.path.join(tmp, "a.wav")
    L.audio_to_wav(audio, wav16)
    wav, _ = sf.read(wav16, dtype="float32")
    mel = L.melspectrogram(wav)
    frames, fps = L.read_frames(face, 0, 0, 25.0)
    boxes = L.detect_boxes(frames, 1.15)
    padded, box, _ = L.pad_for_box(frames[0], boxes[0])
    x1, y1, x2, y2 = box
    print("box", boxes[0], "frame", frames[0].shape, "mel", mel.shape)

    model = L.Wav2Lip()
    sd = torch.load(L.CKPT, map_location="cpu")
    if "state_dict" in sd:
        sd = sd["state_dict"]
    model.load_state_dict({k.replace("module.", ""): v for k, v in sd.items()})
    model.eval()

    chunks = L.build_mel_chunks(mel, fps, start_f + count + 1)
    tiles = []
    for i in range(start_f, start_f + count):
        face_img = cv2.resize(padded[y1:y2, x1:x2], (96, 96))
        arr = face_img.astype(np.float32) / 255.0
        masked = arr.copy()
        masked[48:] = 0
        six = np.concatenate([masked, arr], axis=2).transpose(2, 0, 1)[None]
        audio_b = chunks[i].reshape(1, 1, 80, 16)
        with torch.no_grad():
            pred = model(torch.from_numpy(audio_b), torch.from_numpy(six)).numpy()
        img = (np.clip(pred[0], 0, 1) * 255).astype(np.uint8).transpose(1, 2, 0)
        img = cv2.resize(img, (240, 240), interpolation=cv2.INTER_NEAREST)
        cv2.putText(img, str(i), (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        tiles.append(img)
    rows = [np.hstack(tiles[k:k + 6]) for k in range(0, len(tiles), 6)]
    cv2.imwrite(out, np.vstack(rows))
    print("saved", out)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

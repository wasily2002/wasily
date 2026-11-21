# -*- coding: utf-8 -*-
"""一次性下载 Kokoro multi-lang v1.0 中文语音包（sherpa-onnx 用，来源 hf-mirror）。
只取中文必需文件，跳过无关语言字典。完成后打印 OK 清单。"""
import os
import sys
import urllib.request

REPO = "https://hf-mirror.com/csukuangfj/kokoro-multi-lang-v1_0/resolve/main/"
DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "models", "kokoro-multi-lang-v1_0")

FILES = [
    "model.onnx", "voices.bin", "tokens.txt",
    "lexicon-zh.txt", "lexicon-us-en.txt",
    "date-zh.fst", "number-zh.fst", "phone-zh.fst",
    # jieba 中文分词词典
    "dict/jieba.dict.utf8", "dict/hmm_model.utf8", "dict/idf.utf8",
    "dict/stop_words.utf8", "dict/user.dict.utf8",
    "dict/pos_dict/char_state_tab.utf8", "dict/pos_dict/prob_emit.utf8",
    "dict/pos_dict/prob_start.utf8", "dict/pos_dict/prob_trans.utf8",
    # espeak-ng 最小运行集（普通话 cmn）
    "espeak-ng-data/phondata", "espeak-ng-data/phondata-manifest",
    "espeak-ng-data/phonindex", "espeak-ng-data/phontab",
    "espeak-ng-data/intonations",
    "espeak-ng-data/cmn_dict",
    "espeak-ng-data/lang/sit/cmn", "espeak-ng-data/lang/sit/cmn-Latn-pinyin",
]


def fetch(rel):
    dst = os.path.join(DEST, rel.replace("/", os.sep))
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        print(f"[skip] {rel} ({os.path.getsize(dst)//1024}KB)", flush=True)
        return True
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".part"
    req = urllib.request.Request(REPO + rel, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total and done % (20 << 20) < (1 << 20):
                    print(f"  {rel}: {done*100//total}%", flush=True)
        os.replace(tmp, dst)
        print(f"[ok] {rel} ({os.path.getsize(dst)//1024}KB)", flush=True)
        return True
    except Exception as e:
        print(f"[FAIL] {rel}: {e}", flush=True)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def main():
    print(f"下载目录: {DEST}", flush=True)
    bad = [rel for rel in FILES if not fetch(rel)]
    if bad:
        print("失败文件: " + ", ".join(bad), flush=True)
        sys.exit(1)
    print("ALL_DONE", flush=True)


if __name__ == "__main__":
    main()

SPEC = {"name": "file_zip", "description": "压缩目录为zip", "parameters": {"path": "目录路径", "out": "输出zip路径(可选)"}}
def run(path="", out=""):
    import shutil
    if not path or not os.path.isdir(path): return "请提供有效目录 path"
    if not out: out = path.rstrip("\\/") + ".zip"
    shutil.make_archive(out[:-4], "zip", path)
    return f"已打包: {out}（{os.path.getsize(out)/1048576:.1f} MB）"
import os

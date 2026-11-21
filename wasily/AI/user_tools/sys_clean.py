SPEC = {"name": "sys_clean", "description": "清理系统临时文件与回收站", "parameters": {}}
def run():
    import shutil, tempfile
    freed = 0
    tmp = tempfile.gettempdir()
    for f in os.listdir(tmp):
        p = os.path.join(tmp, f)
        try:
            sz = os.path.getsize(p) if os.path.isfile(p) else 0
            if os.path.isfile(p): os.remove(p)
            elif os.path.isdir(p): shutil.rmtree(p, ignore_errors=True)
            freed += sz
        except Exception: pass
    try:
        import winshell
        winshell.recycle_bin().empty(confirm=False, show_progress=False, sound=False)
    except Exception: pass
    return f"临时目录已清理，释放约 {freed/1048576:.1f} MB（回收站已尝试清空）"
import os

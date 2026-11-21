SPEC = {"name": "todo_list", "description": "待办事项管理", "parameters": {"action": "add/list/done/del", "text": "事项内容或编号"}}
import json as _json
from pathlib import Path as _P
_DB = _P(__file__).parent / "todo_db.json"
def _load():
    return _json.loads(_DB.read_text("utf-8")) if _DB.exists() else []
def _save(items):
    _DB.write_text(_json.dumps(items, ensure_ascii=False, indent=1), "utf-8")
def run(action="list", text=""):
    items = _load()
    if action == "add":
        items.append({"text": text, "done": False}); _save(items)
        return f"已添加: {text}（共{len(items)}项）"
    if action in ("done", "del"):
        i = int(text) - 1
        if 0 <= i < len(items):
            if action == "done": items[i]["done"] = True
            else: items.pop(i)
            _save(items)
            return "已更新"
        return "编号无效"
    lines = [f"{'☑' if t['done'] else '☐'} {i+1}. {t['text']}" for i, t in enumerate(items)]
    return "\n".join(lines) if lines else "清单为空"

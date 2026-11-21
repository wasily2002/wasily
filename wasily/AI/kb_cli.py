# -*- coding: utf-8 -*-
"""知识库命令行：scan / search / stats / vision / serve。

用法（在 wasily/AI 目录下）：
    python kb_cli.py scan                 # 按配置扫描全盘
    python kb_cli.py scan --root D:\\ --force
    python kb_cli.py search 报销发票 --kind image
    python kb_cli.py stats
    python kb_cli.py vision --download    # 下载 SmolVLM-500M
"""
import argparse
import json
import sys
import time


def _fmt(rec, q):
    lines = ["  %s  [%s]" % (rec["name"], rec["kind"]), "    " + rec["path"]]
    for s in rec.get("snippets", [])[:2]:
        lines.append("    · " + s["text"][:180].replace("\n", " "))
    return "\n".join(lines)


def cmd_scan(args):
    import kb_index
    ix = kb_index.get_indexer()
    roots = args.root or None
    res = ix.start(roots=roots, force=args.force)
    if not res.get("ok"):
        print("启动失败:", res.get("error"))
        return 1
    print("索引已启动，根目录:", ", ".join(res["status"]["roots"]))
    if not args.wait:
        return 0
    try:
        while True:
            st = ix.status()
            print("[%s] 发现=%s 已索引=%s 跳过=%s 错误=%s 图片=%s 库=%.1fMB %s" % (
                st.get("phase"), st.get("found"), st.get("indexed"),
                st.get("skipped"), st.get("errors"), st.get("images"),
                st.get("db_mb", 0), (st.get("current") or "")[-70:]), flush=True)
            if not st.get("running"):
                print("完成。", st.get("message") or "")
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        ix.stop()
        print("\n已请求停止。")
        return 0


def cmd_search(args):
    import kb_search
    res = kb_search.search(args.query, kind=args.kind, limit=args.limit)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    if not res.get("ok"):
        print("检索失败:", res.get("error"))
        return 1
    print("命中 %d 个文件 · %dms · 模式 %s" % (res["count"], res["took_ms"], res["mode"]))
    for rec in res["results"]:
        print(_fmt(rec, args.query))
    if not res["results"]:
        print("（无结果，请先运行 scan 建立索引）")
    return 0


def cmd_stats(args):
    import kb_search
    import kb_vlm
    print(json.dumps(kb_search.stats(), ensure_ascii=False, indent=2))
    if args.vision:
        print(json.dumps(kb_vlm.status(), ensure_ascii=False, indent=2))
    return 0


def cmd_vision(args):
    import kb_vlm
    if args.download:
        print("开始下载 SmolVLM-500M-Instruct（约 1.1GB）…")
        ok, msg = kb_vlm.ensure_model(True)
        print("结果:", ok, msg)
        return 0 if ok else 1
    print(json.dumps(kb_vlm.status(), ensure_ascii=False, indent=2))
    return 0


def main():
    p = argparse.ArgumentParser(description="wasily 本地知识库")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="扫描并建立索引")
    s.add_argument("--root", action="append", help="扫描根目录，可重复；默认读配置")
    s.add_argument("--force", action="store_true", help="全部重建（忽略增量）")
    s.add_argument("--wait", action="store_true", help="前台等待并打印进度")
    s.add_argument("--interval", type=float, default=3.0)
    s.set_defaults(func=cmd_scan)

    q = sub.add_parser("search", help="混合检索")
    q.add_argument("query")
    q.add_argument("--kind", default=None, help="text/pdf/docx/xlsx/pptx/image")
    q.add_argument("--limit", type=int, default=10)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_search)

    st = sub.add_parser("stats", help="索引统计")
    st.add_argument("--vision", action="store_true")
    st.set_defaults(func=cmd_stats)

    v = sub.add_parser("vision", help="视觉模型状态/下载")
    v.add_argument("--download", action="store_true")
    v.set_defaults(func=cmd_vision)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()

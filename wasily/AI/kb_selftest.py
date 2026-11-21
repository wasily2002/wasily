# -*- coding: utf-8 -*-
"""知识库功能自测：读取 → 理解 → 检索 → 展示 全流程 + 只读校验。

运行：
    python kb_selftest.py            # 文本/Office/PDF
    python kb_selftest.py --image    # 追加图片 OCR（首次会下载 easyocr 权重）
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
RESULT = {"pass": 0, "fail": 0, "checks": []}


def check(name, ok, detail=""):
    RESULT["pass" if ok else "fail"] += 1
    RESULT["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
    print(("[PASS] " if ok else "[FAIL] ") + name + ("  " + detail if detail else ""))


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    st = os.stat(path)
    return h.hexdigest(), st.st_size, round(st.st_mtime, 3)


def build_corpus(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    (root / "报销制度.txt").write_text(
        "差旅报销制度\n\n员工出差需在五个工作日内提交发票，逾期不予报销。\n"
        "发票金额超过 5000 元需部门经理审批。\n", encoding="utf-8")
    (root / "会议纪要.md").write_text(
        "# 产品评审会\n\n## 结论\n新版点餐小程序在十一月上线。\n"
        "## 风险\n支付接口联调可能延期。\n", encoding="utf-8")
    (root / "data.csv").write_text(
        "月份,营收\n一月,12000\n二月,15800\n", encoding="utf-8")

    import docx
    d = docx.Document()
    d.add_heading("采购合同", level=1)
    d.add_paragraph("乙方若延迟交付，每日按合同总额的千分之三支付违约金。")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "物料"
    t.cell(0, 1).text = "单价"
    t.cell(1, 0).text = "不锈钢餐盘"
    t.cell(1, 1).text = "23.5"
    d.save(str(root / "采购合同.docx"))

    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "库存"
    ws.append(["商品", "数量", "仓库"])
    ws.append(["一次性餐盒", 3200, "上海仓"])
    ws.append(["环保筷子", 1500, "杭州仓"])
    wb.save(str(root / "库存表.xlsx"))

    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "煮饭仔点餐项目"
    box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(1))
    box.text_frame.text = "目标：本地知识库支持跨类型搜索"
    prs.save(str(root / "项目介绍.pptx"))

    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Invoice No. 2026-1109, total 8800 CNY, "
                                "paid by bank transfer.", fontsize=14)
    doc.save(str(root / "invoice.pdf"))
    doc.close()

    if IMAGE_OK:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (520, 200), (250, 250, 246))
        dr = ImageDraw.Draw(img)
        dr.text((24, 40), "INVOICE 2026-1109  TOTAL 8800 CNY", fill=(20, 20, 20))
        dr.rectangle([24, 90, 480, 170], outline=(60, 120, 60), width=3)
        img.save(str(root / "receipt.png"))


def wait_done(ix, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = ix.status()
        if not st.get("running") and st.get("phase") in ("done", "idle"):
            return st
        time.sleep(1)
    return ix.status()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", action="store_true", help="包含图片 OCR 测试")
    args = ap.parse_args()
    global IMAGE_OK
    IMAGE_OK = args.image

    work = Path(tempfile.mkdtemp(prefix="wasily_kb_test_"))
    corpus = work / "corpus"
    os.environ["WASILY_KB_DIR"] = str(work / "kbdata")
    sys.path.insert(0, str(BASE))

    build_corpus(corpus)
    before = {p.name: digest(p) for p in sorted(corpus.iterdir())}
    before_list = sorted(os.listdir(corpus))
    print("测试语料:", before_list)

    import kb_index
    import kb_search
    import kb_store

    ix = kb_index.get_indexer()
    res = ix.start(roots=[str(corpus)], force=True)
    check("索引任务启动", res.get("ok"), res.get("error", ""))
    st = wait_done(ix)
    check("索引完成且无中断", not st.get("running"), json.dumps(
        {k: st.get(k) for k in ("phase", "found", "indexed", "errors", "message")},
        ensure_ascii=False))
    check("全部文件已索引", st.get("indexed", 0) >= 6,
          "indexed=%s" % st.get("indexed"))

    cases = [
        ("报销发票", "报销制度.txt"),
        ("支付接口 延期", "会议纪要.md"),
        ("违约金", "采购合同.docx"),
        ("一次性餐盒", "库存表.xlsx"),
        ("跨类型搜索", "项目介绍.pptx"),
        ("Invoice", "invoice.pdf"),
        ("二月 15800", "data.csv"),
    ]
    for q, expect in cases:
        r = kb_search.search(q, limit=5)
        names = [x["name"] for x in r.get("results", [])]
        check("检索「%s」命中 %s" % (q, expect), expect in names,
              "结果=%s" % names)

    if IMAGE_OK:
        r = kb_search.search("INVOICE 8800", kind="image", limit=5)
        names = [x["name"] for x in r.get("results", [])]
        check("图片内容检索命中 receipt.png", "receipt.png" in names, "结果=%s" % names)

    # 只读校验：哈希、大小、mtime 全部不变，目录里不新增任何文件
    after = {p.name: digest(p) for p in sorted(corpus.iterdir()) if p.name in before}
    check("源文件哈希/大小/mtime 完全未变", before == after,
          "差异=%s" % [k for k in before if before[k] != after.get(k)])
    check("语料目录未新增文件", sorted(os.listdir(corpus)) == before_list,
          str(sorted(os.listdir(corpus))))

    # 展示层：状态统计与文件访问记录
    stats = kb_search.stats()
    check("统计接口可用", stats.get("indexed_files", 0) >= 6, json.dumps(
        {k: stats.get(k) for k in ("indexed_files", "chunks", "chars", "db_mb")},
        ensure_ascii=False))
    rec = kb_store.get_store().get_file_by_path(str(corpus / "采购合同.docx"))
    check("文件可反查（供 Web 打开原文件）", bool(rec), "")

    print("\n==== 汇总：%d 通过 / %d 失败 ====" % (RESULT["pass"], RESULT["fail"]))
    out = work / "kb_selftest_report.json"
    out.write_text(json.dumps(RESULT, ensure_ascii=False, indent=2), encoding="utf-8")
    print("报告:", out)
    shutil.rmtree(work, ignore_errors=True)
    return 0 if RESULT["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

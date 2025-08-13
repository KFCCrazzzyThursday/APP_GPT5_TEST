#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 enriched.json 生成“单词卡片”PDF：每个词两页
- 第1页：只写单词（大标题）
- 第2页：中文、phrases、例句、注意事项(model_notes)

用法：
  python scripts/make_word_cards.py data/outputs/enrich_0.json cards.pdf --limit 200
依赖：
  pip install orjson reportlab
"""
from pathlib import Path
import orjson
import sys
from reportlab.lib.pagesizes import A5
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


def pick_font():
    # 在 Win 上优先用微软雅黑，Linux/mac 可改 NotoSansCJK
    candidates = [
        ("MSYH", "C:/Windows/Fonts/msyh.ttc"),
        ("MSYH", "C:/Windows/Fonts/msyh.ttf"),
        ("NotoSansCJK", "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        ("PingFang", "/System/Library/Fonts/PingFang.ttc"),
    ]
    for name, path in candidates:
        try:
            pdfmetrics.registerFont(TTFont(name, path))
            return name
        except Exception:
            continue
    return "Helvetica"  # 退化（中文可能不显示）


def load_entries(path: Path):
    data = orjson.loads(path.read_bytes())
    if isinstance(data, dict) and "entries" in data:
        data = data["entries"]
    if not isinstance(data, list):
        raise ValueError("bad json")
    return data


def text_or(d, k, default=""):
    v = d.get(k)
    if isinstance(v, str):
        return v
    return default


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("json", type=Path)
    ap.add_argument("pdf",  type=Path)
    ap.add_argument("--limit", type=int, default=0, help="只导出前 N 条（默认全部）")
    args = ap.parse_args()

    font = pick_font()
    title = ParagraphStyle(name="title", fontName=font, fontSize=40,
                           alignment=TA_CENTER, leading=46, textColor=colors.black)
    p_big = ParagraphStyle(name="big",   fontName=font,
                           fontSize=18, alignment=TA_LEFT,   leading=24)
    p_txt = ParagraphStyle(name="txt",   fontName=font,
                           fontSize=12.5, alignment=TA_LEFT, leading=18)

    entries = load_entries(args.json)
    if args.limit > 0:
        entries = entries[:args.limit]

    doc = SimpleDocTemplate(str(args.pdf), pagesize=A5,
                            leftMargin=32, rightMargin=32, topMargin=36, bottomMargin=32)
    story = []

    for e in entries:
        word = e.get("word") or e.get("Word") or ""
        # page 1: only the word
        story.append(Spacer(1, 120))
        story.append(Paragraph(word, title))
        story.append(PageBreak())

        # page 2: details
        meaning = text_or(e, "meaning_zh", text_or(e, "Meaning", ""))
        pos = e.get("pos") or ""
        example = e.get("example") or {}
        ex_en = example.get("en", "")
        ex_zh = example.get("zh", "")
        notes = e.get("model_notes") or ""
        phrases = e.get("phrases") or []

        body = []
        if pos:
            body.append(f"<b>POS</b>：{pos}")
        if meaning:
            body.append(f"<b>中文</b>：{meaning}")
        if phrases:
            lines = []
            for ph in phrases[:6]:
                phs = ph.get("phrase", "")
                zh = ph.get("meaning_zh", "")
                lines.append(f"• {phs} —— {zh}")
            body.append("<b>Phrases</b>：<br/>" + "<br/>".join(lines))
        if ex_en or ex_zh:
            body.append("<b>Example</b>：<br/>" + (ex_en or "") +
                        ("<br/>" + ex_zh if ex_zh else ""))
        if notes:
            body.append("<b>Notes</b>：<br/>" + notes.replace("\n", "<br/>"))

        story.append(Paragraph(word, p_big))
        story.append(Spacer(1, 6))
        for blk in body:
            story.append(Paragraph(blk, p_txt))
            story.append(Spacer(1, 6))
        story.append(PageBreak())

    doc.build(story)
    print(f"✓ saved: {args.pdf.resolve()}")


if __name__ == "__main__":
    main()

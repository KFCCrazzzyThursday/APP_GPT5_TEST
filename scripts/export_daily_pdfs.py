# -*- coding: utf-8 -*-
"""
export_daily_pdfs.py (root-aware, IPA-safe with per-glyph fallback)

Usage examples:
  python scripts/export_daily_pdfs.py
  python scripts/export_daily_pdfs.py --ipa-font "C:\\Windows\\Fonts\\arialuni.ttf"
  python scripts/export_daily_pdfs.py --font "C:\\Windows\\Fonts\\msyh.ttc" --ipa-font "C:\\path\\to\\CharisSIL-Regular.ttf"
"""

import argparse
import datetime
import json
from pathlib import Path
from typing import Dict, List, Optional

# ---------- PDF libs ----------
try:
    from reportlab.lib import pagesizes, colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
except Exception:
    print("❌ Need reportlab. Install: pip install reportlab")
    raise

# ================= Root detection =================


def _looks_like_root(p: Path) -> bool:
    return (p / "data").exists()


def _find_project_root(explicit: Optional[str] = None) -> Path:
    cands: List[Path] = []
    if explicit:
        cands.append(Path(explicit))
    here = Path(__file__).resolve().parent
    cands += [here, here.parent, here.parent.parent]
    cwd = Path.cwd()
    cands += [cwd, cwd.parent, cwd.parent.parent]
    seen = set()
    for base in cands:
        b = base.resolve()
        for _ in range(6):
            k = str(b)
            if k in seen:
                break
            seen.add(k)
            if _looks_like_root(b):
                return b
            if b.parent == b:
                break
            b = b.parent
    if (here / "data").exists():
        return here
    if (cwd / "data").exists():
        return cwd
    return here

# ================= JSON helpers =================


def _safe_read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        txt = path.read_text(encoding="utf-8-sig", errors="ignore")
    except Exception:
        try:
            txt = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None
    if not txt.strip():
        return None
    try:
        obj = json.loads(txt)
        return {"entries": obj} if isinstance(obj, list) else obj
    except Exception:
        return None


def _coerce_str(x) -> str:
    try:
        return str(x) if x is not None else ""
    except Exception:
        return ""

# ================= progress/store helpers =================


def _get_settings(progress_obj: dict) -> dict:
    return (progress_obj or {}).get("settings", {}) or {}


def _last_store_path(progress_obj: dict, root: Path) -> Optional[Path]:
    s = _get_settings(progress_obj).get("last_store")
    if not s:
        return None
    p = Path(s)
    if not p.is_absolute():
        p = (root / p)
    return p if p.exists() else None


def _store_profile(progress_obj: dict, store_path: Path) -> dict:
    stores = (progress_obj or {}).get("stores", {}) or {}
    return stores.get(str(store_path.resolve()), {"days": {}})


def _collect_words_for_day(progress_obj: dict, store_path: Path, day: str) -> List[str]:
    prof = _store_profile(progress_obj, store_path)
    words = (prof.get("days", {}) or {}).get(day, {}).get("words", []) or []
    out, seen = [], set()
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def _entries_by_word(store_obj: dict) -> Dict[str, dict]:
    entries = (store_obj or {}).get("entries") or (
        store_obj or {}).get("words") or []
    m: Dict[str, dict] = {}
    for e in entries:
        ent = e.get("entry") if isinstance(e, dict) else None
        if ent is None:
            ent = e if isinstance(e, dict) else {}
        w = (e.get("word") if isinstance(e, dict) else None) or ent.get("word")
        if w:
            m[str(w)] = ent
    return m


def _pick_entry(word: str, mapping: Dict[str, dict]) -> Optional[dict]:
    if word in mapping:
        return mapping[word]
    lw = word.lower()
    for k, v in mapping.items():
        if k.lower() == lw:
            return v
    return None

# ================= font helpers =================


def _try_register(name: str, path: Path) -> Optional[str]:
    try:
        if not path.exists():
            return None
        pdfmetrics.registerFont(TTFont(name, str(path)))
        return name
    except Exception:
        return None


def register_body_font(font_path: Optional[str] = None) -> str:
    if font_path:
        p = Path(font_path)
        ok = _try_register(p.stem.replace(" ", "_"), p)
        if ok:
            return ok
    probes = [
        ("NotoSansCJKsc-Regular",
         Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf")),
        ("NotoSansCJKsc-Regular",
         Path("/System/Library/Fonts/Supplemental/NotoSansCJKsc-Regular.otf")),
        ("MSYH", Path("C:/Windows/Fonts/msyh.ttc")),
        ("SIMHEI", Path("C:/Windows/Fonts/simhei.ttf")),
        ("SIMSUN", Path("C:/Windows/Fonts/simsun.ttc")),
        ("PingFangSC-Regular", Path("/System/Library/Fonts/PingFang.ttc")),
        ("DejaVuSans", Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")),
        ("ArialUnicodeMS", Path("C:/Windows/Fonts/arialuni.ttf")),
        ("NotoSans-Regular", Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf")),
        ("NotoSans-Regular", Path("C:/Windows/Fonts/NotoSans-Regular.ttf")),
    ]
    for name, p in probes:
        ok = _try_register(name, p)
        if ok:
            return ok
    return "Helvetica"


def register_ipa_fonts(primary_path: Optional[str] = None) -> List[str]:
    """Return a prioritized list of font names for IPA per-glyph fallback."""
    names: List[str] = []
    # 1) explicit primary
    if primary_path:
        p = Path(primary_path)
        nm = p.stem.replace(" ", "_")
        ok = _try_register(nm, p)
        if ok:
            names.append(ok)
    # 2) strong IPA families
    candidates = [
        ("CharisSIL", Path("C:/Program Files/SIL/Fonts/CharisSIL/CharisSIL-Regular.ttf")),
        ("CharisSIL", Path("/usr/share/fonts/truetype/charis/CharisSIL-Regular.ttf")),
        ("DoulosSIL", Path("/usr/share/fonts/truetype/doulos/DoulosSIL-Regular.ttf")),
        ("DoulosSIL", Path("C:/Program Files (x86)/SIL/Doulos SIL/DoulosSIL-Regular.ttf")),
        ("GentiumPlus", Path(
            "/usr/share/fonts/truetype/edu/gentiumplus/GentiumPlus-Regular.ttf")),
        ("DejaVuSans", Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")),
        ("ArialUnicodeMS", Path("C:/Windows/Fonts/arialuni.ttf")),
        ("NotoSans-Regular", Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf")),
        ("NotoSans-Regular", Path("C:/Windows/Fonts/NotoSans-Regular.ttf")),
    ]
    for name, p in candidates:
        ok = _try_register(name, p)
        if ok and ok not in names:
            names.append(ok)
    # final guard: body font will be appended later if needed
    if not names:
        names.append("Helvetica")
    return names


def _font_coverage(font_name: str) -> set:
    """Best-effort coverage set using ReportLab internals."""
    try:
        f = pdfmetrics.getFont(font_name)
        face = getattr(f, "face", None)
        cw = getattr(face, "charWidths", None)
        if isinstance(cw, dict):
            return set(cw.keys())
    except Exception:
        pass
    # unknown -> pretend ASCII only
    return set(range(32, 127))


def _escape_xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def markup_ipa_with_fallback(ipa: str, fonts_priority: List[str], body_font: str) -> str:
    """Split IPA into runs so each glyph is drawn by a font that covers it."""
    cov = {fn: _font_coverage(fn) for fn in fonts_priority}
    cov[body_font] = _font_coverage(body_font)

    def best_font(ch: str) -> str:
        code = ord(ch)
        for fn in fonts_priority:
            if code in cov.get(fn, ()):
                return fn
        # last resort
        for fn in fonts_priority[::-1]:
            if cov.get(fn):
                return fn
        return body_font
    parts = []
    if not ipa:
        return ""
    cur_font = best_font(ipa[0])
    buf = [ipa[0]]
    for ch in ipa[1:]:
        fn = best_font(ch)
        if fn == cur_font:
            buf.append(ch)
        else:
            parts.append((cur_font, "".join(buf)))
            cur_font = fn
            buf = [ch]
    parts.append((cur_font, "".join(buf)))
    # build markup
    out = []
    for fn, seg in parts:
        out.append(f'<font name="{fn}">/{_escape_xml(seg)}/</font>')
    # Merge consecutive segments with identical font if any
    return "".join(out)

# ================= styles =================


def make_styles(body_font: str):
    ss = getSampleStyleSheet()
    for k in list(ss.byName.keys()):
        st = ss[k]
        st.fontName = body_font
    title = ParagraphStyle("TitleZH", parent=ss["Title"], fontName=body_font,
                           fontSize=20, leading=24, spaceAfter=12, textColor=colors.HexColor("#0F172A"))
    wordStyle = ParagraphStyle("Word", parent=ss["Heading2"], fontName=body_font,
                               fontSize=14, leading=18, textColor=colors.HexColor("#0B3B8C"),
                               spaceBefore=8, spaceAfter=4)
    body = ParagraphStyle("Body", parent=ss["BodyText"], fontName=body_font,
                          fontSize=11, leading=16, textColor=colors.HexColor("#111827"))
    small = ParagraphStyle("Small", parent=ss["BodyText"], fontName=body_font,
                           fontSize=9, leading=13, textColor=colors.HexColor("#334155"))
    return {"title": title, "word": wordStyle, "body": body, "small": small}

# ================= render =================


def render_day_pdf(out_pdf: Path, date_str: str, words: List[str], mapping: Dict[str, dict],
                   styles, ipa_fonts: List[str], body_font: str):
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(out_pdf), pagesize=pagesizes.A4,
        leftMargin=36, rightMargin=36, topMargin=42, bottomMargin=42
    )
    story: List = []
    story.append(
        Paragraph(f"{date_str} — Vocabulary (with IPA)", styles["title"]))
    story.append(Paragraph(f"Count: {len(words)}", styles["small"]))
    story.append(Spacer(1, 12))

    for idx, w in enumerate(words, 1):
        ent = _pick_entry(w, mapping) or {}
        word = _coerce_str(ent.get("word") or w)
        ipa = _coerce_str(ent.get("ipa", "")).strip()
        pos = _coerce_str(ent.get("pos", ""))
        meaning_zh = _coerce_str(ent.get("meaning_zh", ""))
        syns = ent.get("synonyms_en") or []
        phrases = ent.get("phrases") or []
        example = ent.get("example") or {}

        head = f"{idx}. <b>{_escape_xml(word)}</b>"
        if ipa:
            head += "  " + markup_ipa_with_fallback(ipa, ipa_fonts, body_font)
        story.append(Paragraph(head, styles["word"]))

        parts = []
        if meaning_zh:
            parts.append(f"中文：{_escape_xml(meaning_zh)}")
        if pos:
            parts.append(f"词性：{_escape_xml(pos)}")
        if parts:
            story.append(Paragraph("；".join(parts), styles["body"]))

        if isinstance(syns, list) and syns:
            story.append(Paragraph(
                "Synonyms: " + ", ".join([_escape_xml(_coerce_str(s)) for s in syns]), styles["small"]))

        if isinstance(phrases, list) and phrases:
            items = []
            for ph in phrases:
                if isinstance(ph, dict):
                    p = _escape_xml(_coerce_str(ph.get("phrase", "")))
                    m = _escape_xml(_coerce_str(ph.get("meaning_zh", "")))
                    txt = f"{p}" + (f" — {m}" if m else "")
                else:
                    txt = _escape_xml(_coerce_str(ph))
                items.append(
                    ListItem(Paragraph(txt, styles["body"]), leftIndent=12))
            story.append(ListFlowable(
                items, bulletType="bullet", start="•", leftIndent=6))

        if isinstance(example, dict) and (example.get("en") or example.get("zh")):
            en = _escape_xml(_coerce_str(example.get("en", "")))
            zh = _escape_xml(_coerce_str(example.get("zh", "")))
            if en:
                story.append(Paragraph(f"<i>{en}</i>", styles["body"]))
            if zh:
                story.append(Paragraph(zh, styles["small"]))

        story.append(Spacer(1, 8))

    doc.build(story)

# ================= main =================


def main():
    import os
    parser = argparse.ArgumentParser(
        description="Export per-day vocabulary PDFs (with IPA). Root-aware & IPA-safe.")
    parser.add_argument("--root", type=str, default="",
                        help="Project root (dir that contains 'data/').")
    parser.add_argument("--progress", type=str, default="",
                        help="Path to progress.json (default: <root>/data/progress.json)")
    parser.add_argument("--store", type=str, default="",
                        help="Path to store.json (default: progress.settings.last_store or <root>/data/store.json)")
    parser.add_argument("--outdir", type=str, default="",
                        help="Output dir (default: <root>/data/cards_by_day)")
    parser.add_argument("--font", type=str, default="",
                        help="BODY font (TTF/OTF) for Chinese/Latin")
    parser.add_argument("--ipa-font", type=str, default="",
                        help="PRIMARY IPA font; will auto-fallback per-glyph")
    parser.add_argument("--since", type=str, default="",
                        help="Start date inclusive YYYY-MM-DD")
    parser.add_argument("--until", type=str, default="",
                        help="End date inclusive YYYY-MM-DD")
    args = parser.parse_args()

    root = _find_project_root(args.root if args.root else None)
    progress_path = Path(args.progress) if args.progress else (
        root / "data" / "progress.json")
    default_store = root / "data" / "store.json"
    outdir = Path(args.outdir) if args.outdir else (
        root / "data" / "cards_by_day")

    progress = _safe_read_json(progress_path) or {}
    if args.store:
        sp = Path(args.store)
        if not sp.is_absolute():
            sp = (root / sp).resolve()
        store_path = sp
    else:
        store_path = _last_store_path(progress, root) or default_store
    store = _safe_read_json(store_path) or {}
    mapping = _entries_by_word(store)

    body_font = register_body_font(args.font if args.font else None)
    ipa_fonts = register_ipa_fonts(args.ipa_font if args.ipa_font else None)
    if body_font not in ipa_fonts:
        ipa_fonts.append(body_font)  # final fallback

    styles = make_styles(body_font)

    print("—— Path Summary ——")
    print("Project root : ", root)
    print("Progress json: ", progress_path)
    print("Store json   : ", store_path)
    print("Output dir   : ", outdir)
    print("BODY font    : ", body_font)
    print("IPA fonts    : ", ipa_fonts)
    print("-------------------")

    prof = _store_profile(progress, store_path)
    days_dict = (prof.get("days", {}) or {})
    if not days_dict:
        print("⚠️ No day records found for this store in progress.json.")
        return

    def in_window(d: str) -> bool:
        try:
            x = datetime.date.fromisoformat(d)
        except Exception:
            return False
        if args.since:
            try:
                if x < datetime.date.fromisoformat(args.since):
                    return False
            except Exception:
                pass
        if args.until:
            try:
                if x > datetime.date.fromisoformat(args.until):
                    return False
            except Exception:
                pass
        return True

    days = sorted([d for d in days_dict.keys() if in_window(d)])
    if not days:
        print("⚠️ No days to export after applying date filters.")
        return

    outdir.mkdir(parents=True, exist_ok=True)
    ok = 0
    for d in days:
        words = _collect_words_for_day(progress, store_path, d)
        if not words:
            continue
        out_pdf = outdir / f"{d}.pdf"
        try:
            render_day_pdf(out_pdf, d, words, mapping,
                           styles, ipa_fonts, body_font)
            ok += 1
            print(f"✅ {d}: {out_pdf}")
        except Exception as e:
            print(f"❌ {d} failed: {e}")

    print("🎉 Done." if ok else "⚠️ No PDFs written.")


if __name__ == "__main__":
    main()

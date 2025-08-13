# -*- coding: utf-8 -*-
from pathlib import Path
from typing import Dict, List, Callable, Optional, Tuple
import re
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
import pdfplumber

# ===== 固定栅格参数（可从 GUI 调整） =====
DEFAULT_LAYOUT = dict(
    word_box_w=94,
    meaning_box_w=118,
    row_h=31,
    left_x0=40, left_y0=106,
    right_x0=300, right_y0=106,
    rows_per_page=21,
    number_w=30
)

_SKIP = {"word", "no.", "meaning", "title:", "date:", "/"}


def _extract_text(page, bbox):
    crop = page.crop(bbox=bbox)
    if crop is None:
        return ""
    txt = crop.extract_text() or ""
    txt = txt.strip().replace("\r", "\n").replace("\n", " ")
    return re.sub(r"\s{2,}", " ", txt)


def _clean_word(raw: str) -> str:
    if raw.lower() in _SKIP:
        return ""
    w = re.sub(r"^\s*\d+\s+", "", raw).strip()
    return re.sub(r"\s*-\s*", "-", w)


def _clean_meaning(raw: str) -> str:
    if raw.lower() in _SKIP:
        return ""
    return raw.replace(" ", "").strip()


def _row_bboxes(cfg: Dict, row_idx: int):
    y0 = cfg["left_y0"] + row_idx * cfg["row_h"]
    y1 = y0 + cfg["row_h"]
    lw = (cfg["left_x0"] + cfg["number_w"], y0, cfg["left_x0"] +
          cfg["number_w"] + cfg["word_box_w"], y1)
    lm = (lw[2], y0, lw[2] + cfg["meaning_box_w"], y1)
    rw = (cfg["right_x0"] + cfg["number_w"], y0, cfg["right_x0"] +
          cfg["number_w"] + cfg["word_box_w"], y1)
    rm = (rw[2], y0, rw[2] + cfg["meaning_box_w"], y1)
    return lw, lm, rw, rm


def _extract_page(pdf_path: str, page_index: int, layout: Dict) -> List[Dict[str, str]]:
    out = []
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_index]
        for row_idx in range(layout["rows_per_page"]):
            lw, lm, rw, rm = _row_bboxes(layout, row_idx)
            w = _clean_word(_extract_text(page, lw))
            m = _clean_meaning(_extract_text(page, lm))
            if w or m:
                out.append({"word": w, "meaning": m})
            w = _clean_word(_extract_text(page, rw))
            m = _clean_meaning(_extract_text(page, rm))
            if w or m:
                out.append({"word": w, "meaning": m})
    return out


def extract_pdf(
    pdf_path: Path,
    layout: Optional[Dict] = None,
    workers: int = 1,
    progress_cb: Optional[Callable[[int, int], None]] = None
) -> List[Dict[str, str]]:
    """
    提取 PDF -> [{"word","meaning"}...]
    - workers>1 时使用多进程
    - progress_cb(done, total) 用于 GUI 进度条；CLI 可忽略
    """
    layout = {**DEFAULT_LAYOUT, **(layout or {})}
    pdf_path = Path(pdf_path)
    with pdfplumber.open(str(pdf_path)) as pdf:
        total_pages = len(pdf.pages)

    if workers <= 1:
        all_rows = []
        done = 0
        for pidx in range(total_pages):
            rows = _extract_page(str(pdf_path), pidx, layout)
            all_rows.extend(rows)
            done += 1
            if progress_cb:
                progress_cb(done, total_pages)
        # 过滤全空
        return [e for e in all_rows if e["word"] or e["meaning"]]

    all_rows: List[Dict[str, str]] = []
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_extract_page, str(pdf_path), pidx, layout)
                for pidx in range(total_pages)]
        for fut in as_completed(futs):
            rows = fut.result()
            all_rows.extend(rows)
            done += 1
            if progress_cb:
                progress_cb(done, total_pages)

    return [e for e in all_rows if e["word"] or e["meaning"]]

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inspect_sampling_probs.py
检查词库抽样概率是否都一样，并打印每个词的权重和概率。

用法：
  python inspect_sampling_probs.py path/to/enriched.json --top 30
  python inspect_sampling_probs.py enriched.json --alpha 0.6 --beta 0.25 --epsilon 1e-6 --csv probs.csv

说明：
  读取数组或 { "entries":[...] } 结构。
  参与计算的字段优先从 e["srs"] 里取（importance/errors/next_due），
  若没有则回落到 e["importance"]/e["errors"]，仍无则设为默认值 1/0/None。

概率公式（和我之前给你的采样脚本一致）：
  weight_i = (importance_i + 0.01) * (1 + alpha*errors_i) * (1 + beta*overdue_days_i)
  prob_i   = weight_i / sum_j weight_j
其中：
  overdue_days_i = max(0, now - next_due_i)（单位：天；next_due_i 解析 ISO 时间）

若所有 weight 完全一致，最终每个 prob 都是 1/N。
"""

import argparse
import datetime as dt
from pathlib import Path
import orjson
import csv


def load_entries(path: Path):
    data = orjson.loads(path.read_bytes())
    if isinstance(data, dict) and "entries" in data:
        data = data["entries"]
    if not isinstance(data, list):
        raise ValueError("input JSON must be a list or {entries:[...]}")
    return data


def to_ts(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        try:
            return dt.datetime.fromisoformat(str(s)).timestamp()
        except Exception:
            return None


def compute_weight(entry, now_ts, alpha, beta):
    srs = entry.get("srs") or {}
    importance = srs.get("importance", entry.get("importance", 1.0))
    errors = srs.get("errors",     entry.get("errors",     0.0))
    next_due = srs.get("next_due",   None)

    try:
        importance = float(importance)
    except Exception:
        importance = 1.0
    try:
        errors = float(errors)
    except Exception:
        errors = 0.0

    overdue_days = 0.0
    nd_ts = to_ts(next_due)
    if nd_ts is not None:
        overdue_days = max(0.0, (now_ts - nd_ts) / (24*3600))

    # 公式（和你的采样器保持一致）
    w = (importance + 0.01) * (1.0 + alpha*errors) * (1.0 + beta*overdue_days)
    return max(0.0, w), importance, errors, overdue_days, next_due


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json", type=Path, help="enriched json")
    ap.add_argument("--alpha", type=float, default=0.6,  help="错误次数权重")
    ap.add_argument("--beta",  type=float, default=0.25, help="逾期天数权重")
    ap.add_argument("--top",   type=int,   default=50,
                    help="只显示前N个最高概率（0=全部）")
    ap.add_argument("--csv",   type=Path,  default=None, help="导出CSV（可选）")
    args = ap.parse_args()

    entries = load_entries(args.json)
    now_ts = dt.datetime.now().timestamp()

    rows = []
    for i, e in enumerate(entries):
        word = e.get("word") or e.get("Word") or f"#{i}"
        w, imp, err, odays, nd = compute_weight(
            e, now_ts, args.alpha, args.beta)
        rows.append({
            "idx": i, "word": word, "importance": imp, "errors": err,
            "overdue_days": round(odays, 3), "next_due": nd, "weight": w
        })

    total_w = sum(r["weight"] for r in rows) or 0.0
    if total_w <= 0:
        print("! 所有权重为0（或JSON缺少数据），无法计算概率。")
        return

    for r in rows:
        r["prob"] = r["weight"] / total_w

    # 诊断信息
    weights = [r["weight"] for r in rows]
    uniq_w = len(set(round(w, 12) for w in weights))
    probs = [r["prob"] for r in rows]
    uniq_p = len(set(round(p, 12) for p in probs))

    print(f"Total entries: {len(rows)}")
    print(f"alpha={args.alpha}, beta={args.beta}")
    print(
        f"weights sum = {total_w:.6f}, unique weights = {uniq_w}, unique probs = {uniq_p}")

    # 如果全相等，给出提示原因
    if uniq_w == 1:
        print("\n⚠️ 检测到所有权重完全相等 → 所有概率也完全相等。最常见原因：")
        print("  1) 所有词都没有 srs.importance/errors/next_due（或取值完全相同）;")
        print("  2) importance 都是默认 1，errors=0，next_due 为空；")
        print("  3) 你还没有做过复习打分，SRS 信息尚未累积。")
        print("解决：给部分词设置不同的 importance / errors / next_due；或提高 alpha/beta。")

    # 排序输出（前N）
    rows_sorted = sorted(rows, key=lambda x: x["prob"], reverse=True)
    show = rows_sorted if args.top <= 0 else rows_sorted[:args.top]

    print("\n# Top items by probability")
    print(f"{'idx':>4}  {'prob':>10}  {'weight':>10}  {'imp':>6}  {'err':>5}  {'overdue(d)':>10}  {'next_due':>20}  word")
    for r in show:
        print(f"{r['idx']:>4}  {r['prob']:>10.6f}  {r['weight']:>10.4f}  {r['importance']:>6.2f}  {r['errors']:>5.2f}  {r['overdue_days']:>10.2f}  {str(r['next_due'] or '-'):>20}  {r['word']}")

    # 最底部几条（可选）
    tail = rows_sorted[-min(10, len(rows_sorted)):]
    print("\n# Bottom items by probability")
    for r in tail:
        print(f"{r['idx']:>4}  {r['prob']:>10.6f}  {r['weight']:>10.4f}  {r['importance']:>6.2f}  {r['errors']:>5.2f}  {r['overdue_days']:>10.2f}  {str(r['next_due'] or '-'):>20}  {r['word']}")

    # 导出CSV（可选）
    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["idx", "word", "prob", "weight",
                       "importance", "errors", "overdue_days", "next_due"])
            for r in rows_sorted:
                w.writerow([r["idx"], r["word"], f"{r['prob']:.8f}", f"{r['weight']:.6f}",
                            r["importance"], r["errors"], r["overdue_days"], r["next_due"] or ""])
        print(f"\n✓ CSV saved: {args.csv.resolve()}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
读取 enriched.json（{"entries":[...]} 或数组），按“统一 SRS 优先级”计算抽样概率并可视化。

统一规则（与应用中一致）：
1) 先到期/过期（due）最高优先；
2) 其次是从未复习过的新词（review_count==0）；
3) 其它词用艾宾浩斯保留率 R(t)=exp(-t/(tau*strength))，其中 strength≈score(EMA 0~1)；
   我们把“统一优先级 priority = due_factor * R(t)”转为权重：weight ∝ 1 / (epsilon + priority)。

最终抽样权重：
  base = 3.0 (due) / 2.0 (new) / 1.0 (others)
  weight = importance * base * (1 / (eps + priority))
  其中 priority = (0.2 if due else 1.0) * R(t)

用法：
  python plot_sampling_probs.py data/outputs/enrich_0.json --top 80 --out prob.png

依赖：
  pip install orjson matplotlib
"""
import argparse
import datetime as dt
import math
from pathlib import Path
import orjson
import matplotlib.pyplot as plt


# ---------- 读入 ----------
def load_entries(path: Path):
    raw = path.read_bytes()
    if not raw:
        return []
    data = orjson.loads(raw)
    if isinstance(data, dict) and "entries" in data:
        data = data["entries"]
    if not isinstance(data, list):
        raise ValueError(
            "bad json: top-level must be list or {'entries': [...]}")
    return data


# ---------- 小工具 ----------
SECONDS_PER_DAY = 86400.0


def to_ts_maybe(x):
    """
    支持多种输入：
      - 数字（视为秒时间戳）
      - ISO 字符串（支持末尾 Z）
      - None / 空串 → 返回 None
    """
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ---------- 统一 SRS 打分（与应用一致） ----------
def srs_like_weight(entry, now_ts: float,
                    tau_days: float = 1.6,
                    due_factor: float = 0.2,
                    eps: float = 1e-6):
    """
    返回该词的抽样“权重”（越大越容易被抽到）。

    数据字段兼容：
      - srs.score (0~1, EMA)
      - srs.last_ts (秒) / srs.next_due_ts (秒)
      - srs.review_count
      - importance（entry 或 srs）
      - 兼容旧字段：srs.next_due (ISO 字符串)
    """
    srs = entry.get("srs") or entry.get("review") or {}
    # 重要度（可选）
    imp = float(srs.get("importance") or entry.get("importance") or 1.0)

    # 复习次数
    rc = 0
    for k in ("review_count", "n", "reviews"):
        if k in srs:
            try:
                rc = int(srs.get(k) or 0)
                break
            except Exception:
                pass

    # 分数（EMA 强度）0~1
    score = float(srs.get("score", srs.get("avg_score", 0.5)))
    strength = clamp(score, 0.05, 1.0)

    # 上次时间与到期时间
    last_ts = to_ts_maybe(srs.get("last_ts"))
    next_due_ts = to_ts_maybe(srs.get("next_due_ts"))
    if next_due_ts is None:
        # 兼容旧字段：ISO
        next_due_ts = to_ts_maybe(srs.get("next_due"))

    # 是否到/过期
    is_due = bool(next_due_ts and now_ts >= next_due_ts)

    # t 天数：新词（rc==0）不按艾宾浩斯递减（设置为 0 天，R=1 → priority 较大 → 再靠 base=2 抬权重）
    if rc == 0:
        t_days = 0.0
    else:
        if not last_ts:
            t_days = 999.0  # 缺失时视为很久没碰
        else:
            t_days = max(0.0, (now_ts - last_ts) / SECONDS_PER_DAY)

    # 艾宾浩斯保留率：越小越易忘
    R = math.exp(- t_days / (tau_days * strength))

    # 统一优先级（与应用 srs.priority 一致的思想）
    priority = (due_factor if is_due else 1.0) * R  # 越小越紧急

    # 三层池加权：due > new > others
    base = 3.0 if is_due else (2.0 if rc == 0 else 1.0)

    # 转成概率权重（越小的 priority → 越大的权重）
    w = imp * base * (1.0 / (eps + priority))
    return max(1e-12, float(w))


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json", type=Path)
    ap.add_argument("--top", type=int, default=80, help="只画前 N 个概率最高的词（默认80）")
    ap.add_argument("--out", type=Path, default=Path("prob.png"))
    ap.add_argument("--tau", type=float, default=1.6,
                    help="艾宾浩斯时间尺度 τ（天），默认1.6")
    ap.add_argument("--due-factor", type=float, default=0.2,
                    help="到/过期优先因子，越小表示更优先（默认0.2）")
    args = ap.parse_args()

    entries = load_entries(args.json)
    now_ts = dt.datetime.now().timestamp()

    weights = []
    for e in entries:
        word = e.get("word") or e.get("Word") or e.get("entry", {}).get("word")
        if not word:
            continue
        w = srs_like_weight(e, now_ts, tau_days=args.tau,
                            due_factor=args.due_factor)
        weights.append((word, w))

    if not weights:
        print("No entries or all entries missing 'word'.")
        return

    # 归一化为概率
    total_w = sum(w for _, w in weights)
    probs = [(w / total_w) for _, w in weights]
    pairs = list(zip([w for w, _ in weights], probs))
    pairs.sort(key=lambda x: x[1], reverse=True)
    pairs = pairs[:args.top]

    labels = [p[0] for p in pairs]
    vals = [p[1] for p in pairs]

    plt.figure(figsize=(10, max(6, len(vals) * 0.22)))
    cmap = plt.cm.get_cmap("viridis")
    vmax = max(vals) if vals else 1.0
    colors = [cmap((v / vmax) if vmax > 0 else 0.0) for v in vals]
    y = list(range(len(vals)))
    plt.barh(y, vals, color=colors)
    plt.yticks(y, labels, fontsize=9)
    plt.gca().invert_yaxis()
    plt.xlabel("Sampling probability (unified SRS)")
    plt.title("Word Sampling Probabilities — due > new > Ebbihaus(score)")
    plt.tight_layout()
    plt.savefig(args.out, dpi=180)
    print(f"✓ saved: {args.out.resolve()}")


if __name__ == "__main__":
    main()

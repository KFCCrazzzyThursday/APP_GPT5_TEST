# -*- coding: utf-8 -*-
import argparse
from pathlib import Path
from tqdm import tqdm

from gui.app import run as gui_run
from extractor.pdf_extract import extract_pdf
from enrich.enrich import enrich_file
from utils.jsonio import dump_json_atomic


def cli():
    ap = argparse.ArgumentParser(description="Vocab Suite (GUI + CLI)")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("gui", help="启动图形界面（默认）")

    p_x = sub.add_parser("extract", help="PDF -> output.json")
    p_x.add_argument("pdf", type=Path)
    p_x.add_argument("out", type=Path)
    p_x.add_argument("--workers", type=int, default=4)

    p_e = sub.add_parser("enrich", help="output.json -> enriched.json")
    p_e.add_argument("input_json", type=Path)
    p_e.add_argument("output_json", type=Path)
    p_e.add_argument("--batch-size", type=int, default=4)
    p_e.add_argument("--checkpoint-every", type=int, default=20)
    p_e.add_argument("--only-fix-missing", action="store_true")

    args = ap.parse_args()
    if args.cmd in (None, "gui"):
        gui_run()
    elif args.cmd == "extract":
        done = 0
        total = [0]

        def progress(d, t):
            nonlocal done
            done = d
            total[0] = t
            pbar.n = d
            pbar.total = t
            pbar.refresh()
        pbar = tqdm(total=0, desc="Extracting")
        rows = extract_pdf(args.pdf, workers=max(
            1, args.workers), progress_cb=progress)
        pbar.close()
        dump_json_atomic(args.out, {"meta": {"source": str(
            args.pdf.resolve()), "count": len(rows)}, "entries": rows})
        print(f"✓ Saved {len(rows)} entries -> {args.out}")
    elif args.cmd == "enrich":
        done = 0
        total = [0]

        def progress(d, t):
            nonlocal done
            done = d
            total[0] = t
            pbar.n = d
            pbar.total = t
            pbar.refresh()
        pbar = tqdm(total=0, desc="Enriching")
        res = enrich_file(args.input_json, args.output_json, batch_size=args.batch_size,
                          checkpoint_every=args.checkpoint_every, only_fix_missing=args.only_fix_missing,
                          progress_cb=progress, show_tqdm=False)
        pbar.close()
        print(f"✓ Enriched -> {res}")


if __name__ == "__main__":
    cli()

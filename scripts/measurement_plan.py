"""Assign products to a manual block and an AI-assisted block for the review-time measurement.

    python scripts/measurement_plan.py [--per-block 12] [--session 1]

Reads the working database read-only and match_review/candidates.json.  Writes
evaluation_data/measurement_plan_session<N>.csv and prints, for each block, the product numbers to paste
into the search box of the VLM 검토 tab.

Design
  * Different products in the two blocks: nobody reviews a product whose answer they already know.
    Products used as ground-truth cases, and products of earlier sessions, are left out.
  * Difficulty is balanced, not assumed equal: products are grouped by (has a model code on the Musinsa side,
    number of candidates: 1 / 2-4 / 5+) and dealt alternately to the two blocks inside each group.
  * Order effects: odd sessions do the manual block first, even sessions the assisted block first,
    and the order inside a block is shuffled with a fixed seed.
The plan only prepares the measurement.  It records no time and makes no decision.
"""
import argparse
import csv
import glob
import json
import os
import random
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from shop_comparator.matching.match_review import norm_code, usable_code  # noqa: E402
from shop_comparator.services import comparison_service as cmp  # noqa: E402

OUT = os.path.join(ROOT, "evaluation_data")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-block", type=int, default=12)
    parser.add_argument("--session", type=int, default=1)
    args = parser.parse_args()
    db = sqlite3.connect(f"file:{os.path.join(ROOT, 'musinsa_data.sqlite')}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    matches = {r["musinsa_goods_no"]: dict(r) for r in db.execute("SELECT * FROM kream_search_cache")}
    with open(os.path.join(ROOT, "match_review", "candidates.json"), encoding="utf-8") as f:
        candidates = json.load(f)
    used = set()
    try:
        with open(os.path.join(OUT, "gold", "cases.json"), encoding="utf-8") as f:
            used |= {c["musinsa"]["goodsNo"] for c in json.load(f)["cases"]}
    except OSError:
        pass
    for earlier in glob.glob(os.path.join(OUT, "measurement_plan_session*.csv")):
        if not earlier.endswith(f"session{args.session}.csv"):
            with open(earlier, encoding="utf-8-sig", newline="") as f:
                used |= {row["goodsNo"] for row in csv.DictReader(f)}
    reviewed = set()
    try:
        reviewed = {r[0] for r in db.execute("SELECT DISTINCT goodsNo FROM match_reviews")}
    except sqlite3.OperationalError:
        pass
    pool = {}
    for goods_no, entry in candidates.items():
        info = cmp.match_info(matches.get(goods_no))
        if goods_no in used or goods_no in reviewed or not entry.get("candidates") or info["kind"] in ("rule", "user"):
            continue                     # rule- or user-confirmed products are not review work
        count = len(entry["candidates"])
        group = ("code" if usable_code(norm_code(entry.get("serial"))) else "nocode", "1" if count == 1 else "2-4" if count <= 4 else "5+")
        pool.setdefault(group, []).append(goods_no)
    rng = random.Random(20261010 + args.session)
    blocks = {"A": [], "B": []}
    for group in sorted(pool):
        items = sorted(pool[group])
        rng.shuffle(items)
        for index, goods_no in enumerate(items[: len(items) // 2 * 2]):          # an even number per group keeps the blocks balanced
            blocks["AB"[index % 2]].append((goods_no, "/".join(group)))
    for name in blocks:
        rng.shuffle(blocks[name])
        blocks[name] = blocks[name][: args.per_block]
    size = min(len(blocks["A"]), len(blocks["B"]))
    blocks = {name: items[:size] for name, items in blocks.items()}
    first, second = ("manual", "assisted") if args.session % 2 else ("assisted", "manual")
    plan = [("A", first, 1), ("B", second, 2)]
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"measurement_plan_session{args.session}.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["session", "order", "block", "mode", "position", "goodsNo", "difficultyGroup"])
        for block, mode, order in plan:
            for position, (goods_no, group) in enumerate(blocks[block], 1):
                writer.writerow([args.session, order, block, mode, position, goods_no, group])
    print(f"세션 {args.session}: 블록당 {size}개 (검수 대상 후보 {sum(len(v) for v in pool.values())}개 중, 정답 사례·이전 세션·이미 검수한 상품 제외)")
    for block, mode, order in plan:
        label = "수동 (AI 제안 숨기기 켬)" if mode == "manual" else "AI 보조 (AI 제안 숨기기 끔)"
        print(f"\n{order}번째: 블록 {block} · {label}\n  검색창에 붙여넣기: {' '.join(g for g, _ in blocks[block])}")
        groups = {}
        for _, group in blocks[block]:
            groups[group] = groups.get(group, 0) + 1
        print("  난이도 묶음:", ", ".join(f"{k} {v}" for k, v in sorted(groups.items())))
    print("\n저장:", path)


if __name__ == "__main__":
    main()

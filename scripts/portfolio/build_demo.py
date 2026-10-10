"""Build an isolated demo folder for portfolio captures.

    python scripts/portfolio/build_demo.py [--out portfolio_demo]

The working database is copied with SQLite's backup API (it is only read).  In
the copy: login cookies, sessions, webhook settings and job history are removed,
review tables start empty, and "the batch collected last" is set to a fixed list
of products that have stored KREAM candidates, thumbnails and VLM verdicts.
Nothing here contacts a shop or an API.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Products chosen to show the review cases worth looking at (all from the 2026-10-09 collection).
FEATURED = [
    "3976350",   # Air Force 1 white: two candidates share the model code; the VLM picks the regular edition
    "4207634",   # no model code on KREAM; VLM suggests the same product
    "3157671",   # Reebok Club C 85: shop code on Musinsa, VLM suggests the right product
    "2200892",   # Blundstone 585
    "4313969",   # Timberland: VLM says "same" for a different model line (known wrong suggestion)
    "7044931",   # different brand suggested as "same" (known wrong suggestion)
    "5042975",   # model-code rule and VLM pick different candidates
    "7407733",   # model-code match that the VLM calls "different"
]
REOPENED = {}      # nothing is unmatched for the demo: matches registered by a model already wait for review


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join(ROOT, "portfolio_demo"))
    parser.add_argument("--source", default=ROOT)
    parser.add_argument("--size", type=int, default=40)
    args = parser.parse_args()
    out, src = os.path.abspath(args.out), os.path.abspath(args.source)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "match_review", "images"))

    source_db = sqlite3.connect(f"file:{os.path.join(src, 'musinsa_data.sqlite')}?mode=ro", uri=True)
    demo_db = sqlite3.connect(os.path.join(out, "musinsa_data.sqlite"))
    source_db.backup(demo_db)
    source_db.close()

    with open(os.path.join(src, "match_review", "candidates.json"), encoding="utf-8") as f:
        candidates = json.load(f)
    with open(os.path.join(src, "match_review", "vlm_results.json"), encoding="utf-8") as f:
        verdicts = json.load(f)
    judged = [g for g, e in candidates.items() if e.get("candidates") and e.get("musinsaImage")
              and verdicts.get(g, {}).get("decision") in ("same", "different", "uncertain")
              and (verdicts[g].get("at") or "") >= (e.get("collectedAt") or "")]
    known = {r[0] for r in demo_db.execute("SELECT goodsNo FROM products")}
    matched = {r[0]: r[1] for r in demo_db.execute("SELECT musinsa_goods_no, match_method FROM kream_search_cache WHERE match_verified = 1")}
    judged = [g for g in judged if g in known]
    batch = [g for g in FEATURED if g in judged]
    by_kind = {"same": [], "different": [], "uncertain": []}
    for g in judged:
        if g not in batch:
            by_kind[verdicts[g]["decision"]].append(g)
    coded = [g for g in by_kind["same"] if matched.get(g) == "model_code"]
    open_same = [g for g in by_kind["same"] if matched.get(g) != "model_code"]
    batch += coded[:14] + open_same[:8] + by_kind["different"][:8] + by_kind["uncertain"][:2]
    batch = batch[:args.size]

    for table in ("cookies", "site_sessions", "job_runs", "monitor_items", "job_items", "vlm_suggestions", "match_reviews"):
        try:
            demo_db.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            pass
    demo_db.execute("DELETE FROM web_settings WHERE key LIKE '%webhook%' OR key LIKE '%alert%'")
    for goods_no in REOPENED:
        demo_db.execute("DELETE FROM kream_search_cache WHERE musinsa_goods_no = ?", (goods_no,))
        demo_db.execute("UPDATE products SET kream_product_id = NULL, kream_product_url = NULL WHERE goodsNo = ?", (goods_no,))
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    demo_db.execute("INSERT OR REPLACE INTO web_settings (key, value) VALUES ('last_collection', ?)",
                    (json.dumps({"goods": batch, "at": stamp}),))
    demo_db.execute("UPDATE web_product_state SET active = 1")
    demo_db.commit()
    demo_db.execute("VACUUM")
    demo_db.close()

    with open(os.path.join(out, "match_review", "candidates.json"), "w", encoding="utf-8") as f:
        json.dump({g: candidates[g] for g in batch}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out, "match_review", "vlm_results.json"), "w", encoding="utf-8") as f:
        json.dump({g: verdicts[g] for g in batch}, f, ensure_ascii=False, indent=1)
    for g in batch:
        folder = os.path.join(src, "match_review", "images", g)
        if os.path.isdir(folder):
            shutil.copytree(folder, os.path.join(out, "match_review", "images", g))
    info = {"builtAt": stamp, "source": "2026-10-09 실제 수집분(무신사 신발 랭킹)과 저장된 gpt-5.4-mini 판정",
            "products": len(batch), "batch": batch, "featured": [g for g in FEATURED if g in batch], "reopened": REOPENED,
            "removed": ["cookies", "site_sessions", "webhook·alert 설정", "job_runs", "monitor_items"],
            "network": "쇼핑몰과 API를 호출하지 않음"}
    with open(os.path.join(out, "DEMO_INFO.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    print("demo folder:", out, "| products:", len(batch))


if __name__ == "__main__":
    sys.exit(main())

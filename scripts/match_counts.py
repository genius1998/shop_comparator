"""Explain the match numbers shown in the app: which rows, which products, which review state.

    python scripts/match_counts.py [--db musinsa_data.sqlite] [--save]

The database is opened read-only.  --save writes docs/evaluation/integration/match_counts.json.
"""
import argparse
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from shop_comparator.services import comparison_service as cmp  # noqa: E402

KIND_LABEL = {"rule": "규칙 확정", "user": "사람이 지정·승인", "ai_unreviewed": "AI 등록(미검수)", "rejected": "검수 거절", "unverified": "미검증(근거 없는 예전 ID)"}


def count(db_file):
    db = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    columns = {r[1] for r in db.execute("PRAGMA table_info(kream_search_cache)")}
    rows = [dict(r) for r in db.execute("SELECT * FROM kream_search_cache")]
    products = {str(r[0]) for r in db.execute("SELECT goodsNo FROM products")}
    active = {str(r[0]) for r in db.execute("""SELECT p.goodsNo FROM products p LEFT JOIN web_product_state s ON s.goodsNo = p.goodsNo
                                               WHERE COALESCE(s.active, 1) = 1""")}
    quotes = {str(r[0]) for r in db.execute("SELECT DISTINCT kreamId FROM kream_quotes")}
    priced = {str(r[0]) for r in db.execute("SELECT DISTINCT goodsNo FROM musinsa_variants WHERE finalPrice IS NOT NULL")}
    try:
        reviews = db.execute("SELECT COUNT(*) FROM match_reviews").fetchone()[0]
    except sqlite3.OperationalError:
        reviews = 0
    scopes = {"allRows": {}, "inProductList": {}, "activeProducts": {}}
    old_verified = new_usable = old_targets = new_targets = 0
    for row in rows:
        info = cmp.match_info(row)
        if info["kind"] == "none":
            continue
        goods_no, key = str(row["musinsa_goods_no"]), f"{info['kind']}/{row.get('match_method') or '-'}"
        scopes["allRows"][key] = scopes["allRows"].get(key, 0) + 1
        if goods_no in products:
            scopes["inProductList"][key] = scopes["inProductList"].get(key, 0) + 1
        if goods_no in active:
            scopes["activeProducts"][key] = scopes["activeProducts"].get(key, 0) + 1
            verified = int(row.get("match_verified") or 0) == 1
            comparable = info["kreamId"] in quotes and goods_no in priced
            old_verified += verified
            new_usable += info["usable"]
            old_targets += verified and comparable
            new_targets += info["usable"] and comparable

    def total(scope, kind):
        return sum(v for k, v in scopes[scope].items() if k.startswith(kind + "/"))

    return {
        "database": os.path.basename(db_file), "hasReviewColumns": "review_state" in columns,
        "cacheRows": len(rows), "productsInList": len(products), "activeProducts": len(active),
        "byKindAndMethod": scopes,
        "aiRegistered": {"allRows": total("allRows", "ai_unreviewed"), "inProductList": total("inProductList", "ai_unreviewed"),
                         "activeProducts": total("activeProducts", "ai_unreviewed"),
                         "explain": "AI 등록 건수는 범위에 따라 다르다. '전체 행'은 kream_search_cache의 모든 행(예전 GUI가 남긴, 지금 상품 목록에 없는 "
                                    "무신사 상품의 행 포함), '상품 목록'은 그중 현재 products 표에 있는 상품의 행이다."},
        "matchedShownInApp": {"before": old_verified, "after": new_usable,
                              "explain": "화면 상단의 'KREAM 매칭 N개'는 숨기지 않은 상품 중 매칭된 상품 수다. 이전에는 match_verified=1이면 셌고, "
                                         "지금은 규칙 확정 또는 사람이 지정·승인한 것만 센다. 차이는 활성 상품의 AI 등록(미검수) 수와 같다."},
        "priceComparisonTargets": {"before": old_targets, "after": new_targets,
                                   "explain": "가격 비교 대상 = 활성 상품 + 사용할 수 있는 매칭 + KREAM 시세 수집됨 + 무신사 회원가 있음."},
        "humanReviewRecordsInThisDatabase": reviews,
        "labels": KIND_LABEL,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.path.join(ROOT, "musinsa_data.sqlite"))
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()
    report = count(args.db)
    if args.save:
        out = os.path.join(ROOT, "docs", "evaluation", "integration")
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "match_counts.json"), "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

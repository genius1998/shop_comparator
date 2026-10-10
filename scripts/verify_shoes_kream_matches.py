# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import contextlib
import io
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shop_comparator.core import config
from shop_comparator.shops import crawler
from shop_comparator.storage import database
from shop_comparator.shops import kream_crawler
from shop_comparator.matching.product_matcher import extract_style_codes, load_local_env, select_best_kream_match


def _safe_log(message: str) -> None:
    print(message, flush=True)


def _normalize_id(value: Any) -> str | None:
    try:
        if value is None or pd.isna(value):
            return None
        value_str = str(value).strip()
        if value_str in {"", "0", "0.0", "nan"}:
            return None
        return str(int(float(value_str)))
    except (TypeError, ValueError):
        return None


def _verified_cache_for(goods_no: str) -> dict[str, Any] | None:
    cached = database.get_cached_kream_search(goods_no)
    if cached and cached.get("kream_product_id") and int(cached.get("match_verified") or 0) == 1:
        return cached
    return None


def _cached_kream_metadata(kream_id: str | None) -> dict[str, Any]:
    if not kream_id:
        return {}

    with database.get_db_conn() as conn:
        row = conn.execute(
            """
            SELECT brand_name, product_name, model_number
            FROM kream_products
            WHERE product_id = ?
            """,
            (str(kream_id),),
        ).fetchone()

    if not row:
        return {}

    return {
        "kream_brand": row["brand_name"],
        "kream_product_name": row["product_name"],
        "kream_model_number": row["model_number"],
    }


def _fetch_shoes_ranking_products(top_n: int) -> list[dict[str, Any]]:
    session = database.load_auth_session()
    cookie_header = session.get("cookie_header") or ""
    auth_payload = session.get("auth_payload") or {}
    if not cookie_header:
        raise RuntimeError("Musinsa login cookie is missing. Login before running this report.")
    sess = crawler.build_session(cookie_header, auth_payload=auth_payload)

    query_params = config.SHOES_RANKING_LAYOUT.split("?", 1)[1]
    section_id = None
    for part in query_params.split("&"):
        if part.startswith("sectionId="):
            section_id = part.split("=", 1)[1]
            break
    if not section_id:
        raise RuntimeError("SHOES_RANKING_LAYOUT에서 sectionId를 찾지 못했습니다.")

    base_url = f"https://api.musinsa.com/api2/hm/web/v5/pans/ranking/sections/{section_id}"
    rank_items: list[tuple[int, str, str]] = []
    seen = set()
    page = 1
    while len(rank_items) < top_n:
        offset = len(rank_items)
        params = f"{query_params}&page={page}&startRank={offset + 1}&offset={offset}"
        res = sess.get(f"{base_url}?{params}", timeout=20)
        res.raise_for_status()
        page_items = crawler._pick_items_from_sections_api(res.json())
        if not page_items:
            break
        added = 0
        for item in page_items:
            goods_no = item[1]
            if goods_no in seen:
                continue
            seen.add(goods_no)
            rank_items.append(item)
            added += 1
            if len(rank_items) >= top_n:
                break
        if added == 0:
            break
        page += 1

    rank_items = sorted(rank_items, key=lambda item: item[0])[:top_n]
    ids = [goods_no for _, goods_no, _ in rank_items]
    details = crawler.fetch_details(sess, ids)

    products: list[dict[str, Any]] = []
    for rank, goods_no, link in rank_items:
        detail = details.get(str(goods_no), {})
        with contextlib.redirect_stdout(io.StringIO()):
            priced = crawler.process_one(
                sess,
                cookie_header,
                str(goods_no),
                link,
                detail,
                sleep_each=0.2,
                auth_payload=auth_payload,
            )
        goods_name = priced.get("goodsName") or detail.get("goodsName") or ""
        style_codes = extract_style_codes(goods_name)
        products.append(
            {
                "rank": rank,
                "goodsNo": str(priced.get("goodsNo") or detail.get("goodsNo") or goods_no),
                "linkUrl": priced.get("linkUrl") or detail.get("linkUrl") or link,
                "goodsName": goods_name,
                "brand": detail.get("brand"),
                "brandName": priced.get("brandName") or detail.get("brandName"),
                "serial_number": priced.get("serial_number") or (style_codes[0] if style_codes else ""),
                "imageUrl": priced.get("imageUrl") or detail.get("imageUrl"),
                "normalPrice": priced.get("normalPrice") or detail.get("normalPrice"),
                "salePrice": priced.get("salePrice"),
                "couponDiscount": priced.get("couponDiscount"),
                "memberDiscountAmount": priced.get("memberDiscountAmount"),
                "pointDiscountAmount": priced.get("pointDiscountAmount"),
                "finalPrice": priced.get("finalPrice"),
                "memberDiscountRate": priced.get("memberDiscountRate"),
                "memberSavePointRate": priced.get("memberSavePointRate"),
                "isMemberDiscount": priced.get("isMemberDiscount"),
                "isUsePointDiscount": priced.get("isUsePointDiscount"),
                "goodsSavePoint": priced.get("goodsSavePoint"),
                "couponName": priced.get("couponName"),
                "couponValueKind": priced.get("couponValueKind"),
                "managedCode": priced.get("managedCode"),
                "goodsOptionNo": priced.get("goodsOptionNo"),
                "availableSizes": priced.get("availableSizes"),
                "musinsa_price_source": "logged_in_order_form" if not priced.get("error") else "fallback_detail",
                "musinsa_price_error": priced.get("error"),
                "reviewCount": detail.get("reviewCount"),
                "reviewScore": detail.get("reviewScore"),
                "isSoldOut": priced.get("soldout", detail.get("isSoldOut")),
            }
        )
    return products


def _match_product(product: dict[str, Any]) -> dict[str, Any]:
    goods_no = str(product.get("goodsNo") or "").strip()
    musinsa_name = product.get("goodsName") or ""
    brand = product.get("brandName") or ""
    serial = product.get("serial_number") or ""
    image_url = product.get("imageUrl") or ""

    cached = _verified_cache_for(goods_no)
    if cached:
        kream_id = _normalize_id(cached.get("kream_product_id"))
        metadata = _cached_kream_metadata(kream_id)
        return {
            "kream_product_id": kream_id,
            "kream_product_url": cached.get("kream_product_url") or (f"https://kream.co.kr/products/{kream_id}" if kream_id else ""),
            "kream_brand": cached.get("kream_brand") or metadata.get("kream_brand"),
            "kream_product_name": cached.get("kream_product_name") or metadata.get("kream_product_name"),
            "kream_model_number": metadata.get("kream_model_number"),
            "kream_image_url": cached.get("kream_image_url"),
            "kream_all_text": cached.get("kream_all_text"),
            "match_status": "matched_cached",
            "match_source": "verified_cache",
            "match_method": cached.get("match_method"),
            "match_confidence": cached.get("match_confidence"),
            "match_reason": cached.get("match_reason"),
            "verified_at": cached.get("verified_at"),
        }

    queries: list[str] = []
    if serial:
        queries.append(serial)
    if musinsa_name and musinsa_name not in queries:
        queries.append(musinsa_name)
    queries = queries[:2]

    last_reason = "No KREAM candidate passed local/embedding/LLM verification."
    last_method = None
    last_confidence = 0.0
    sess = requests.Session()

    for query in queries:
        kream_crawler._sleep_jitter(0.6, 1.2)
        data = kream_crawler._search_once(sess, query)
        candidates = kream_crawler._extract_products_from_screen(data)
        if not candidates:
            last_reason = f"KREAM search returned no candidates for query: {query}"
            continue

        decision = select_best_kream_match(
            {
                "name": musinsa_name,
                "brand": brand,
                "serial_number": serial,
                "image_url": image_url,
            },
            candidates,
        )
        last_reason = decision.reason
        last_method = decision.method
        last_confidence = decision.confidence
        if not decision.matched or not decision.product:
            continue

        matched_product = decision.product
        kream_id = _normalize_id(decision.product.get("id"))
        kream_url = f"https://kream.co.kr/products/{kream_id}" if kream_id else ""
        result = kream_crawler.KreamSearchResult(
            kream_id,
            match_verified=True,
            match_method=decision.method,
            match_confidence=decision.confidence,
            match_reason=decision.reason,
            kream_brand=matched_product.get("brand"),
            kream_product_name=matched_product.get("name_ko"),
            kream_image_url=matched_product.get("image_url"),
            kream_all_text=matched_product.get("all_text"),
        )
        database.cache_kream_search(
            goods_no,
            result,
            kream_url,
            kream_brand=matched_product.get("brand"),
            kream_product_name=matched_product.get("name_ko"),
            kream_image_url=matched_product.get("image_url"),
            kream_all_text=matched_product.get("all_text"),
        )
        if serial and kream_id:
            kream_crawler.save_product_to_db(
                int(kream_id),
                serial,
                matched_product.get("brand") or brand,
                matched_product.get("name_ko") or "",
            )
        return {
            "kream_product_id": kream_id,
            "kream_product_url": kream_url,
            "kream_brand": matched_product.get("brand"),
            "kream_product_name": matched_product.get("name_ko"),
            "kream_model_number": None,
            "kream_image_url": matched_product.get("image_url"),
            "kream_all_text": matched_product.get("all_text"),
            "match_status": "matched_new",
            "match_source": f"search:{query}",
            "match_method": decision.method,
            "match_confidence": decision.confidence,
            "match_reason": decision.reason,
            "verified_at": None,
        }

    if not last_method:
        last_method = "search"

    return {
        "kream_product_id": None,
        "kream_product_url": "",
        "kream_brand": None,
        "kream_product_name": None,
        "kream_model_number": None,
        "kream_image_url": None,
        "kream_all_text": None,
        "match_status": "not_matched",
        "match_source": "search",
        "match_method": last_method,
        "match_confidence": last_confidence,
        "match_reason": last_reason,
        "verified_at": None,
    }


def run(top_n: int, output_dir: Path) -> Path:
    load_local_env()
    database.init_db()

    # Avoid printing request headers/cookies from crawler._log_api_call.
    crawler._log_api_call = lambda *args, **kwargs: None

    _safe_log(f"[START] Musinsa shoes ranking crawl: top_n={top_n}")
    products = _fetch_shoes_ranking_products(top_n)
    _safe_log(f"[CRAWL] collected={len(products)}")

    rows: list[dict[str, Any]] = []
    for index, product in enumerate(products, start=1):
        goods_no = str(product.get("goodsNo") or "")
        _safe_log(f"[MATCH] {index}/{len(products)} goodsNo={goods_no} name={product.get('goodsName')}")
        match = _match_product(product)
        rows.append(
            {
                "rank": product.get("rank"),
                "musinsa_goods_no": goods_no,
                "musinsa_brand": product.get("brandName"),
                "musinsa_product_name": product.get("goodsName"),
                "musinsa_serial_number": product.get("serial_number"),
                "musinsa_url": product.get("linkUrl"),
                "musinsa_image_url": product.get("imageUrl"),
                "musinsa_sale_price": product.get("salePrice"),
                "musinsa_coupon_discount": product.get("couponDiscount"),
                "musinsa_member_discount": product.get("memberDiscountAmount"),
                "musinsa_point_discount": product.get("pointDiscountAmount"),
                "musinsa_final_price": product.get("finalPrice"),
                "musinsa_coupon_name": product.get("couponName"),
                "musinsa_coupon_value": product.get("couponValueKind"),
                "musinsa_member_discount_rate": product.get("memberDiscountRate"),
                "musinsa_member_save_point_rate": product.get("memberSavePointRate"),
                "musinsa_available_sizes": product.get("availableSizes"),
                "musinsa_price_source": product.get("musinsa_price_source"),
                "musinsa_price_error": product.get("musinsa_price_error"),
                **match,
            }
        )
        _safe_log(
            f"[MATCH_RESULT] goodsNo={goods_no} status={match['match_status']} "
            f"kream_id={match['kream_product_id']} method={match['match_method']} "
            f"confidence={match['match_confidence']}"
        )
        _safe_log(f"[MATCH_REASON] goodsNo={goods_no} reason={match['match_reason']}")

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"musinsa_shoes_kream_match_{top_n}_{stamp}.xlsx"

    result_df = pd.DataFrame(rows)
    summary_df = (
        result_df.groupby(["match_status", "match_method"], dropna=False)
        .size()
        .reset_index(name="count")
        .sort_values(["match_status", "match_method"], na_position="last")
    )

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        result_df.to_excel(writer, index=False, sheet_name="matches")
        summary_df.to_excel(writer, index=False, sheet_name="summary")

    _safe_log(f"[DONE] saved={output_path}")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-n", type=int, default=100)
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    args = parser.parse_args()
    run(args.top_n, args.output_dir)


if __name__ == "__main__":
    main()

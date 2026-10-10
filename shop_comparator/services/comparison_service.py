"""Size-level comparison built from stored Musinsa and KREAM results.

Nothing here talks to a shop.  Rows are computed from the database each
time, so a failed collection on one side never erases the other side.
"""
import json
import os
import re
from urllib.parse import urlparse

from shop_comparator.storage import store
from shop_comparator.core.crawl_errors import CrawlError

PRICE_NOTE = ("무신사 가격은 해당 옵션 1개 주문서 기준(쿠폰·회원 할인·적립금 선할인 반영, 배송비·결제 프로모션 미반영). "
              "KREAM 가격은 표시 판매가 기준(검수비·수수료·배송비 미포함).")


def product_id(value, service):
    value = str(value).strip()
    if re.fullmatch(r"[1-9]\d{0,11}", value):
        return value
    parsed = urlparse(value)
    allowed = {"musinsa.com", "www.musinsa.com"} if service == "musinsa" else {"kream.co.kr", "www.kream.co.kr"}
    match = re.fullmatch(r"/(?:products|app/goods)/(\d+)/?", parsed.path)
    if parsed.scheme == "https" and parsed.hostname in allowed and match:
        return match[1]
    raise ValueError(f"{'무신사' if service == 'musinsa' else 'KREAM'} 상품번호 또는 상품 URL을 입력해 주세요.")


_ONE_SIZE = {"FREE", "F", "FR", "OS", "ONESIZE", "ONE", "1SIZE", "프리", "원사이즈"}


def size_key(value):
    """Canonical key for matching sizes of the SAME size system only.

    mm sizes: "250", "250mm", KREAM's gendered "W250"/"M250" -> "MM:250".
    Regional sizes keep their system: "US 8" -> "US:8", "UK 8" -> "UK:8".
    Apparel letters are normalised (2XL -> XXL).  Anything else matches
    only an identical string.  No cross-system conversion is ever guessed.
    """
    text = re.sub(r"\s+", "", str(value or "").upper())
    if not text:
        return ""
    m = re.fullmatch(r"[WM]?(\d{3}(?:\.5)?)(?:MM)?", text)
    if m and 150 <= float(m[1]) <= 340:
        return "MM:" + m[1]
    m = re.fullmatch(r"(US|UK|EU|JP|KR|IT|FR)[-:]?([WMK]?)(\d+(?:\.\d+)?)", text)
    if m:
        number = m[3].rstrip("0").rstrip(".") if "." in m[3] else m[3]
        if m[1] == "KR":
            return "MM:" + number
        return f"{m[1]}{m[2]}:{number}"
    if text in _ONE_SIZE:
        return "ONE"
    m = re.fullmatch(r"(\d)(X+)([SL])", text)
    if m:
        return "A:" + "X" * int(m[1]) * len(m[2]) + m[3]
    if re.fullmatch(r"X{0,4}[SML]", text):
        return "A:" + text
    return "=:" + text


def size_system(key):
    return {"MM": "mm", "A": "의류", "ONE": "단일", "=": "원문 일치"}.get(key.split(":")[0], key.split(":")[0])


def trust_unverified():
    return os.getenv("KREAM_TRUST_EXISTING_MATCHES", "").strip().lower() in {"1", "true", "yes", "on"}


AI_LABELS = {"openai": "언어 모델(자동 판별기)", "embedding": "임베딩 유사도(자동 판별기)", "image_review": "AI 보조 이미지 검토"}


def match_info(match):
    """Describe a kream_search_cache row for display and decide if it may be used for prices.

    kind      rule           decided by a rule (model code, name/brand/code scorer)
              user           typed or approved by a person
              ai_unreviewed  registered by a model or an AI-assisted review: kept, shown, NOT used until a person approves
              rejected       a person rejected it: kept, not used
              unverified     an old id without any record of how it was matched
    """
    if not match or not match.get("kream_product_id") or str(match["kream_product_id"]) in ("", "0", "None"):
        return {"kreamId": "", "status": "매칭 없음", "usable": False, "confidence": None, "method": "", "reason": "", "kind": "none"}
    verified = int(match.get("match_verified") or 0) == 1
    method = match.get("match_method") or ""
    origin = match.get("origin_method") or ""
    if match.get("review_state") == "rejected":
        kind, status, usable = "rejected", "검수 거절", False
    elif method == "manual":
        kind, status, usable = "user", "직접 지정", True
    elif method == "human_review" and verified:
        kind, status, usable = "user", "검수 승인", True
    elif method == "approved" and verified:
        kind, status, usable = "user", "사용자 승인", True
    elif method == "model_code" and verified:
        kind, status, usable = "rule", "모델번호 일치", True
    elif method == "local" and verified:
        kind, status, usable = "rule", "규칙 확정", True
    elif verified:
        # a model's judgement alone never confirms a match, whatever confidence it reported
        kind, status, usable = "ai_unreviewed", "AI 등록(미검수)", False
    else:
        kind, status, usable = "unverified", "미검증", trust_unverified()
    return {
        "kreamId": str(match["kream_product_id"]).split(".")[0], "status": status, "kind": kind,
        "usable": usable, "confidence": match.get("match_confidence"),
        "method": method, "reason": match.get("match_reason") or "",
        "source": AI_LABELS.get(method, method) if kind == "ai_unreviewed" else AI_LABELS.get(origin, origin),
        "originMethod": origin, "originReason": match.get("origin_reason") or "",
        "kreamName": match.get("kream_product_name") or "", "kreamBrand": match.get("kream_brand") or "",
        "verifiedAt": match.get("verified_at"),
    }


def _variant_status(variant):
    if variant.get("soldout"):
        return "품절"
    status = variant.get("priceStatus")
    if status == "verified":
        return None
    if status == "stale":
        return "회원가 갱신 실패(이전 값)"
    if status == "error":
        return "회원가 확인 실패"
    return "회원가 미조회"


def compare_product(product, variants, match, quotes, poizon=None, kream_state=None):
    """Rows for one product: every Musinsa size, then KREAM-only sizes."""
    info = match_info(match)
    poizon = poizon or {}
    kream_state = kream_state or {}
    buckets = {}
    for quote in quotes if info["usable"] else []:
        buckets.setdefault(size_key(quote["size"]), []).append(quote)
    used = set()
    rows = []
    base = {
        "goodsNo": str(product["goodsNo"]), "goodsName": product.get("goodsName") or "",
        "brandName": product.get("brandName") or "", "rank": product.get("rank"),
        "serial": product.get("serial_number") or "", "kreamId": info["kreamId"],
        "musinsaUrl": f"https://www.musinsa.com/products/{product['goodsNo']}",
        "kreamUrl": f"https://kream.co.kr/products/{info['kreamId']}" if info["kreamId"] else "",
        "matchStatus": info["status"], "matchConfidence": info["confidence"], "matchMethod": info["method"],
        "kreamError": kream_state.get("kreamError") if kream_state.get("kreamStatus") == "error" else "",
    }
    for variant in sorted(variants, key=lambda v: _sort_key(v.get("size"))):
        key = size_key(variant.get("size"))
        candidates = buckets.get(key, []) if key else []
        quote = candidates[0] if len(candidates) == 1 else None
        if quote:
            used.add(quote["size"])
        final = variant.get("finalPrice") if variant.get("priceStatus") in ("verified", "stale") else None
        status = _variant_status(variant)
        if status is None or status == "회원가 갱신 실패(이전 값)":
            if not info["kreamId"]:
                kream_status = "상품 매칭 필요"
            elif not info["usable"]:
                kream_status = "매칭 검증 필요"
            elif not quotes:
                kream_status = "KREAM 조회 실패" if base["kreamError"] else "KREAM 미수집"
            elif len(candidates) > 1:
                kream_status = "KREAM 사이즈 중복"
            elif quote is None:
                kream_status = "KREAM에 없는 사이즈"
            elif quote.get("instantBuy") is None:
                kream_status = "즉시구매 불가(입찰만)" if quote.get("bidOnly") else "즉시구매가 없음"
            else:
                kream_status = None
            status = status or kream_status or "비교 완료"
            if status == "회원가 갱신 실패(이전 값)" and kream_status:
                status = f"{status} · {kream_status}"
        buy = quote.get("instantBuy") if quote else None
        sell = quote.get("instantSell") if quote else None
        poizon_price = poizon.get(key) if key else None
        rows.append({**base,
            "size": variant.get("size") or "", "sizeKey": key, "sizeSystem": size_system(key) if key else "",
            "optionNo": variant.get("optionNo"), "soldout": bool(variant.get("soldout")),
            "salePrice": variant.get("salePrice"), "coupon": variant.get("couponName") or "",
            "couponDiscount": variant.get("couponDiscount"), "memberDiscount": variant.get("memberDiscountAmount"),
            "pointDiscount": variant.get("pointDiscountAmount"), "musinsaPrice": final,
            "priceStatus": variant.get("priceStatus") or "", "priceSource": variant.get("priceSource") or "",
            "musinsaCheckedAt": variant.get("checkedAt") or variant.get("stockCheckedAt"),
            "musinsaError": variant.get("error") or "",
            "kreamSize": quote["size"] if quote else "", "kreamBuy": buy,
            "kreamBuyDelivery": (quote.get("instantBuyDelivery") or "") if quote else "",
            "kreamSell": sell, "kreamCheckedAt": quote.get("checkedAt") if quote else None,
            "difference": buy - final if buy is not None and final is not None else None,
            "sellDifference": sell - final if sell is not None and final is not None else None,
            "poizonPrice": poizon_price,
            "poizonDifference": poizon_price - final if poizon_price is not None and final is not None else None,
            "status": status, "kreamOnly": False,
        })
    for quote in sorted((q for q in quotes if info["usable"] and q["size"] not in used), key=lambda q: _sort_key(q["size"])):
        key = size_key(quote["size"])
        if len(buckets.get(key, [])) > 1 and any(r["sizeKey"] == key for r in rows):
            continue
        rows.append({**base,
            "size": "", "sizeKey": key, "sizeSystem": size_system(key), "optionNo": None, "soldout": False,
            "salePrice": None, "coupon": "", "couponDiscount": None, "memberDiscount": None, "pointDiscount": None,
            "musinsaPrice": None, "priceStatus": "", "priceSource": "", "musinsaCheckedAt": None, "musinsaError": "",
            "kreamSize": quote["size"], "kreamBuy": quote.get("instantBuy"),
            "kreamBuyDelivery": quote.get("instantBuyDelivery") or "", "kreamSell": quote.get("instantSell"),
            "kreamCheckedAt": quote.get("checkedAt"), "difference": None, "sellDifference": None,
            "poizonPrice": None, "poizonDifference": None, "status": "무신사에 없는 사이즈", "kreamOnly": True,
        })
    return rows


def _sort_key(size):
    text = str(size or "")
    m = re.search(r"\d+(?:\.\d+)?", text)
    order = {"XXS": 0, "XS": 1, "S": 2, "M": 3, "L": 4, "XL": 5, "XXL": 6, "XXXL": 7}
    if text.upper() in order:
        return (1, order[text.upper()], text)
    return (0, float(m[0]), text) if m else (2, 0, text)


def build(goods_nos=None, include_kream_only=True):
    """Comparison rows for the active list (or the given products)."""
    products = store.products(active_only=goods_nos is None, goods_nos=goods_nos)
    matches = store.all_matches()
    variants, quotes, poizon = {}, {}, {}
    for v in store.variants():
        variants.setdefault(v["goodsNo"], []).append(v)
    for q in store.kream_quotes():
        try:
            q["candidates"] = json.loads(q.get("candidates") or "[]")
        except ValueError:
            q["candidates"] = []
        quotes.setdefault(q["kreamId"], []).append(q)
    for p in store.poizon_quotes():
        poizon.setdefault(p["goodsNo"], {})[size_key(p["size"])] = p["price"]
    rows = []
    for product in products:
        goods_no = str(product["goodsNo"])
        match = matches.get(goods_no)
        info = match_info(match)
        product_rows = compare_product(product, variants.get(goods_no, []), match,
                                       quotes.get(info["kreamId"], []) if info["kreamId"] else [],
                                       poizon.get(goods_no), product)
        if not include_kream_only:
            product_rows = [r for r in product_rows if not r["kreamOnly"]]
        rows.extend(product_rows)
    return rows


def summarize(rows):
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"rows": len(rows), "products": len({r["goodsNo"] for r in rows}),
            "compared": counts.get("비교 완료", 0), "byStatus": counts}


def require_products(goods_nos=None):
    products = store.products(active_only=goods_nos is None, goods_nos=goods_nos)
    if not products:
        raise CrawlError("비교", "prerequisite", "상품 목록이 비어 있습니다. 먼저 무신사 수집을 실행해 주세요.")
    return products


def safe_error(exc):
    if isinstance(exc, (CrawlError, ValueError)):
        return str(exc)
    return f"{type(exc).__name__}: 수집을 완료하지 못했습니다. 로그인/네트워크 상태를 확인해 주세요."

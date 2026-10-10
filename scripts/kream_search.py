# kream_search.py
# 사용 예)
#   set KREAM_BEARER=your-kream-access-token
#   python kream_search.py --keyword 1174470-BLK --out resp.json
#
# (PowerShell)
#   $env:KREAM_BEARER="your-kream-access-token"
#   python .\kream_search.py --keyword 1174470-BLK

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import requests


KST = timezone(timedelta(hours=9))


def kream_client_datetime_kst(dt: Optional[datetime] = None) -> str:
    # 예: 20260106144513+0900
    dt = dt or datetime.now(KST)
    return dt.strftime("%Y%m%d%H%M%S%z")


def build_headers(
    bearer: str,
    device_id: str,
    web_build_version: str,
    api_version: str,
    web_request_secret: str,
    user_agent: str,
    referer_keyword: str,
) -> Dict[str, str]:
    return {
        "accept": "*/*",
        "accept-language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "authorization": f"Bearer {bearer}",
        "origin": "https://kream.co.kr",
        "referer": f"https://kream.co.kr/search?keyword={referer_keyword}&tab=products&footer=home",
        "user-agent": user_agent,
        "x-kream-api-version": api_version,
        "x-kream-client-datetime": kream_client_datetime_kst(),
        "x-kream-device-id": device_id,
        "x-kream-web-build-version": web_build_version,
        "x-kream-web-request-secret": web_request_secret,
        # 아래는 브라우저 힌트 헤더라 없어도 되는 경우가 많지만,
        # 서버가 깐깐할 수 있어서 원하면 추가 가능:
        # "sec-fetch-dest": "empty",
        # "sec-fetch-mode": "cors",
        # "sec-fetch-site": "same-site",
    }


def fetch_search_products(
    keyword: str,
    bearer: str,
    *,
    tab: str = "products",
    typed_string: Optional[str] = None,
    search_type: str = "direct",
    request_key: Optional[str] = None,
    device_id: str = "web;d61f82c5-98cb-4cc9-afb9-674a883b97c3",
    web_build_version: str = "25.15.6",
    api_version: str = "52",
    web_request_secret: str = "kream-djscjsghdkd",
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    timeout_sec: int = 20,
) -> Dict[str, Any]:
    url = "https://api.kream.co.kr/api/screens/search/products"

    params = {
        "keyword": keyword,
        "tab": tab,
        "typed_string": typed_string or keyword,
        "search_type": search_type,
        "request_key": request_key or str(uuid.uuid4()),
    }

    headers = build_headers(
        bearer=bearer,
        device_id=device_id,
        web_build_version=web_build_version,
        api_version=api_version,
        web_request_secret=web_request_secret,
        user_agent=user_agent,
        referer_keyword=keyword,
    )

    with requests.Session() as s:
        resp = s.get(url, params=params, headers=headers, timeout=timeout_sec)
        # 디버깅 편하게
        if resp.status_code != 200:
            raise RuntimeError(
                f"HTTP {resp.status_code}\n"
                f"URL: {resp.url}\n"
                f"Response (first 500): {resp.text[:500]}"
            )

        # 응답이 JSON이면 보통 이게 맞음
        try:
            return resp.json()
        except Exception:
            # 혹시라도 JSON 파싱이 안 되면 raw 텍스트로라도 전달
            return {"raw": resp.text, "url": resp.url}

def extract_products(data: dict) -> list[dict]:
    """
    KREAM search 응답(UI JSON)에서 product_card만 추출해서
    상품 핵심 정보를 리스트로 리턴
    """
    out = []
    content = (data or {}).get("content") or {}
    items = content.get("items") or []

    for it in items:
        if (it or {}).get("display_type") != "product_card":
            continue

        # product_id: "product_card/595625" or meta 안에 있을 수 있음
        raw_id = (it.get("id") or "")
        product_id = raw_id.split("/")[-1] if "/" in raw_id else raw_id

        # 클릭 이벤트 properties JSON에 더 많은 정보가 있음
        # 근데 지금 샘플에선 properties가 string(JSON)이라 파싱이 필요할 수도 있음.
        # 여기선 UI에 확실히 있는 텍스트/URL 중심으로 긁어옴.
        product_url = None
        for act in (it.get("actions") or []):
            if act.get("type") == "url" and isinstance(act.get("value"), str):
                product_url = act["value"]
                break

        # 카드 내부 텍스트들 훑어서 브랜드/이름/가격 같이 찾기
        brand = None
        name_ko = None
        price_text = None
        image_url = None

        def walk(node):
            nonlocal brand, name_ko, price_text, image_url
            if not isinstance(node, dict):
                return
            dt = node.get("display_type")

            # 이미지
            if dt in ("thumbnail", "image_body"):
                img = node.get("image_element") or (node.get("image_item") or {}).get("image_element")
                if isinstance(img, dict):
                    dv = img.get("default_variation") or {}
                    if isinstance(dv.get("url"), str) and not image_url:
                        image_url = dv["url"]

            # 텍스트
            if dt == "text_body":
                te = node.get("text_element") or {}
                dv = te.get("default_variation") or {}
                t = dv.get("text")
                if isinstance(t, str):
                    # 브랜드는 보통 "UGG" 같이 짧게 뜸
                    if not brand and len(t) <= 20 and t.isupper():
                        brand = t
                    # 상품명(한글)
                    if not name_ko and ("(" in t or "어그" in t or "블랙" in t):
                        name_ko = t
                    # 가격
                    if not price_text and ("원" in t and any(ch.isdigit() for ch in t)):
                        price_text = t

            # 하위 아이템 재귀
            for child in (node.get("items") or []):
                walk(child)
            for child in (node.get("image_item"), node.get("label1_item"), node.get("right_item")):
                if isinstance(child, dict):
                    walk(child)

        walk(it)

        out.append(
            {
                "product_id": product_id,
                "brand": brand,
                "name_ko": name_ko,
                "price_text": price_text,
                "product_url": product_url,
                "image_url": image_url,
            }
        )

    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", required=True, help="예: 1174470-BLK")
    ap.add_argument("--typed-string", default=None, help="기본값: keyword와 동일")
    ap.add_argument("--tab", default="products")
    ap.add_argument("--search-type", default="direct")
    ap.add_argument("--request-key", default=None, help="기본값: 랜덤 UUID")
    ap.add_argument("--out", default=None, help="저장할 파일 경로 (예: resp.json)")
    ap.add_argument("--bearer-env", default="KREAM_BEARER", help="Bearer 토큰 환경변수명")
    args = ap.parse_args()

    bearer = os.getenv(args.bearer_env)
    if not bearer:
        print(
            f"[ERROR] 환경변수 {args.bearer_env} 에 Bearer 토큰을 넣어줘.\n"
            f"예) Windows CMD: set {args.bearer_env}=your-kream-access-token\n"
            f"    PowerShell: $env:{args.bearer_env}='your-kream-access-token'\n",
            file=sys.stderr,
        )
        return 2

    data = fetch_search_products(
        keyword=args.keyword,
        bearer=bearer,
        tab=args.tab,
        typed_string=args.typed_string,
        search_type=args.search_type,
        request_key=args.request_key,
    )

    text = json.dumps(data, ensure_ascii=False, indent=2)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"OK. saved => {args.out}")
    else:
        print(text)

    products = extract_products(data)
    print(f"FOUND: {len(products)}")
    for p in products:
        print(p)


    return 0


if __name__ == "__main__":
    raise SystemExit(main())

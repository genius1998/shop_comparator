import requests
import json
import os
from bs4 import BeautifulSoup

BASE_URL = "https://kr.poizon.com"

def fetch_product_detail_html(url: str) -> str:
    """상품 상세 페이지 HTML 요청"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "close",
    }

    cookies = {}
    cookie_file = "poizon_cookies.json"
    if os.path.exists(cookie_file):
        try:
            with open(cookie_file, "r", encoding="utf-8") as f:
                cookies = json.load(f)
                # 디버깅: du_token 확인 (로그인 여부의 핵심)
                du_token = cookies.get("du_token")
                if du_token:
                    print(f"[POIZON DEBUG] du_token 발견 (앞 10자리): {du_token[:10]}...")
                else:
                    print("[POIZON DEBUG] 경고: 쿠키 파일에 du_token이 없습니다. 비로그인 상태일 수 있습니다.")
        except Exception as e:
            print(f"[POIZON PRICE] 쿠키 파일 로드 실패: {e}")

    resp = requests.get(url, headers=headers, cookies=cookies, timeout=20)
    resp.raise_for_status()
    return resp.text

def get_prices_from_html(html: str, log_callback=print) -> dict:
    """상세페이지 HTML에서 사이즈별 가격을 파싱하여 딕셔너리로 반환"""
    soup = BeautifulSoup(html, "html.parser")

    next_data_tag = soup.find("script", id="__NEXT_DATA__")
    if not next_data_tag:
        log_callback("[POIZON PRICE] __NEXT_DATA__ 스크립트를 찾지 못했습니다.")
        log_callback(f"[POIZON PRICE] 응답 HTML (앞 1000자): {html[:1000]}")
        return {}

    try:
        next_data = json.loads(next_data_tag.string)
        page_props = next_data.get("props", {}).get("pageProps", {})
        
        price_info = page_props.get("priceInfo")
        if price_info:
            try:
                with open("poizon_price_api_result.txt", "w", encoding="utf-8") as f:
                    f.write(json.dumps(price_info, indent=2, ensure_ascii=False))
                log_callback("[POIZON PRICE] 'poizon_price_api_result.txt' 파일에 가격 API 결과를 저장했습니다.")
            except Exception as e:
                log_callback(f"[POIZON PRICE] 가격 API 결과 파일 저장 실패: {e}")

        skus = price_info.get("skus", [])
        property_value_map = page_props.get("propertyValueMap", {})
        sale_properties = price_info.get("saleProperties", [])

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log_callback(f"[POIZON PRICE] JSON 데이터 경로(priceInfo, skus 등)를 찾지 못했습니다: {e}")
        if 'next_data' in locals():
            log_callback(f"[POIZON PRICE] 수신된 __NEXT_DATA__ JSON 전체: {json.dumps(next_data, ensure_ascii=False, indent=2)}")
        return {}

    # --- 사이즈 속성의 propertyId 찾기 (가장 신뢰도 높은 방법) ---
    size_property_id = None
    for prop in sale_properties:
        if prop.get("propertyName") == "사이즈":
            size_property_id = prop.get("propertyId")
            break
    # --- ---

    size_price_map = {}

    for sku in skus:
        try:
            size = None
            # 1순위: saleProperties를 통해 찾은 ID로 사이즈 추출
            if size_property_id:
                for prop in sku.get("properties", []):
                    if prop.get("propertyId") == size_property_id:
                        size_id = str(prop.get("propertyValueId"))
                        if size_id in property_value_map:
                            size = property_value_map[size_id].get("value")
                        break
            
            # 2순위: level: 2로 사이즈 추출 (Fallback)
            if not size:
                for prop in sku.get("properties", []):
                    if prop.get("level") == 2:
                        size_id = str(prop.get("propertyValueId"))
                        if size_id in property_value_map:
                            size = property_value_map[size_id].get("value")
                            if size:
                                log_callback(f"[POIZON PRICE] WARN: size_property_id를 찾지 못해 level:2 기준으로 사이즈 추출: '{size}'")
                            break
            
            # 3순위: skuTitle 파싱 (최후의 Fallback)
            if not size:
                title = sku.get("skuTitle", "")
                last_space_index = title.rfind(' ')
                if last_space_index != -1:
                    size = title[last_space_index+1:].strip()
                else:
                    size = title
                log_callback(f"[POIZON PRICE] WARN: 구조적 사이즈 추출 실패. skuTitle 기반으로 대체: '{title}' -> '{size}'")

            if not size:
                log_callback(f"[POIZON PRICE] ERROR: SKU에서 사이즈를 추출할 수 없습니다: {sku}")
                continue
            
            # 최종 정리: 사이즈 값에서 불필요한 문자 제거
            cleaned_size = size.strip('() ')

            base = sku["minPrice"]
            base_amount = int(base["money"]["amount"])

            promo_amount = base_amount
            speed_infos = sku.get("skuSpeedInfo") or []
            for info in speed_infos:
                promo = info.get("promotionPrice")
                if promo:
                    promo_amount = int(promo["money"]["amount"])
                    break

            if cleaned_size not in size_price_map or promo_amount < size_price_map[cleaned_size]:
                size_price_map[cleaned_size] = promo_amount
        except (KeyError, IndexError, ValueError) as e:
            log_callback(f"[POIZON PRICE] SKU 처리 중 오류 발생: {sku}, 오류: {e}")
            continue
            
    log_callback(f"[POIZON PRICE] {len(size_price_map)}개의 사이즈 가격 정보를 찾았습니다.")
    return size_price_map

def get_poizon_prices(url: str, log_callback=print) -> dict:
    """URL에서 사이즈별 가격 정보를 가져옵니다."""
    try:
        log_callback(f"[POIZON PRICE] URL에서 가격 정보 추출 시작: {url}")
        html = fetch_product_detail_html(url)
        prices = get_prices_from_html(html, log_callback=log_callback)
        return prices
    except requests.exceptions.RequestException as e:
        log_callback(f"[POIZON PRICE] HTML 요청 실패: {e}")
        return {}
    except Exception as e:
        log_callback(f"[POIZON PRICE] 알 수 없는 오류 발생: {e}")
        return {}


if __name__ == "__main__":
    test_url = (
        "https://kr.poizon.com/product/ugg-tasman-slipper-chestnut-women-s-59192122"
        "?track_referer_page_id=2307&track_referer_block_type=4776&track_referer_position=1"
    )
    
    prices = get_poizon_prices(test_url)
    
    if prices:
        print("사이즈별 가격 정보:")
        for size, price in sorted(prices.items(), key=lambda item: float(item[0])):
            print(f"  - 사이즈: {size}, 가격: {price:,}원")
    else:
        print("가격 정보를 가져오지 못했습니다.")
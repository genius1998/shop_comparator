import requests
from bs4 import BeautifulSoup
import json
import os

BASE_URL = "https://kr.poizon.com"

def fetch_search_html(keyword: str, cookies: dict = None) -> str:
    """
    포이즌 검색 결과 페이지 HTML을 리턴 (requests 사용)
    """
    url = f"https://kr.poizon.com/search?keyword={keyword}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    
    resp = requests.get(url, headers=headers, cookies=cookies, timeout=20)
    resp.raise_for_status()
    # 디버깅: 응답 내용 일부 출력
    # print(f"[DEBUG] 검색 응답 HTML (앞 500자):\n{resp.text[:500]}\n")
    return resp.text

def search_poizon(keyword: str, cookies: dict = None) -> list:
    """
    키워드로 포이즌 검색 후, 상위 결과 리스트(딕셔너리) 반환
    """
    # 쿠키가 인자로 전달되지 않았다면 파일에서 로드 시도
    if cookies is None and os.path.exists("poizon_cookies.json"):
        try:
            with open("poizon_cookies.json", "r", encoding="utf-8") as f:
                cookies = json.load(f)
        except Exception as e:
            print(f"[POIZON SEARCH] 쿠키 로드 실패: {e}")

    try:
        html = fetch_search_html(keyword, cookies=cookies)
        soup = BeautifulSoup(html, "html.parser")
        
        # __NEXT_DATA__ 스크립트 찾기
        next_data_tag = soup.find("script", id="__NEXT_DATA__")
        if not next_data_tag:
            return []
        
        data = json.loads(next_data_tag.string)
        
        # 검색 결과 경로 탐색
        # props -> pageProps -> spuList (리스트) - 기존 searchList에서 spuList로 변경
        # example.txt 분석 결과 spuList에 상품 정보가 있음
        page_props = data.get("props", {}).get("pageProps", {})
        search_list = page_props.get("spuList", [])
        
        results = []
        for item in search_list:
            # 필요한 정보 추출
            # title: 상품명
            # spuId: 상품 ID
            # logoUrl: 이미지 URL (없으면 image 키 확인)
            # minSpuPrice: 가격 정보 객체
            
            product_id = item.get("spuId")
            title = item.get("title")
            image = item.get("logoUrl") or item.get("image")
            
            # 가격 정보 추출 (구조가 다를 수 있으므로 안전하게 접근)
            price = None
            min_price_obj = item.get("minSpuPrice")
            if min_price_obj and isinstance(min_price_obj, dict):
                money_obj = min_price_obj.get("money")
                if money_obj and isinstance(money_obj, dict):
                    price = money_obj.get("amount")
            
            # 상품 상세 URL 생성
            # 포이즌 상세 페이지 패턴: https://kr.poizon.com/product/{spuId}
            # 또는 title 등을 조합
            if product_id:
                product_url = f"/product/{str(title).replace(' ', '-').lower() if title else 'product'}-{product_id}"
            else:
                product_url = None
            
            if product_id and title:
                results.append({
                    "id": product_id,
                    "name": title,
                    "image": image,
                    "price": price,
                    "url": product_url
                })
                
        return results

    except Exception as e:
        print(f"[POIZON SEARCH] Error searching for '{keyword}': {e}")
        return []

if __name__ == "__main__":
    test_keyword = "1174470-BLK"
    print(f"Testing search with keyword: {test_keyword}")
    
    cookies = None
    if os.path.exists("poizon_cookies.json"):
        try:
            with open("poizon_cookies.json", "r", encoding="utf-8") as f:
                cookies = json.load(f)
            print("Loaded cookies from poizon_cookies.json")
        except Exception as e:
            print(f"Failed to load cookies: {e}")

    res = search_poizon(test_keyword, cookies=cookies)
    print(json.dumps(res, indent=2, ensure_ascii=False))
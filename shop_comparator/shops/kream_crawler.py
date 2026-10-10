# -*- coding: utf-8 -*-

from __future__ import annotations

import time, json, uuid, re, os, base64, random, sqlite3, csv
import subprocess
import requests
import requests.adapters
import shutil
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode, quote

# 로컬 모듈
from shop_comparator.core.crawl_errors import CrawlError, check_status
from shop_comparator.shops.device_id import get_device_id
from shop_comparator.core.utils import resource_path, get_persistent_path
from shop_comparator.shops.proxy_manager import proxy_manager
from shop_comparator.matching.product_matcher import select_best_kream_match

# --- 전역 상수 ---
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
KREAM_API_VERSION     = "52"
KREAM_WEB_REQUEST_SEC = "kream-djscjsghdkd"
KREAM_WEB_BUILD_VER   = "25.15.7"
KREAM_DEVICE_ID       = get_device_id(get_persistent_path("device_id.txt"))
SENTRY_TRACE = ""
BAGGAGE = ""
DB_PATH = get_persistent_path('musinsa_data.sqlite')


class KreamSearchResult(int):
    def __new__(
        cls,
        value,
        match_verified: bool = False,
        match_method: str | None = None,
        match_confidence: float | None = None,
        match_reason: str | None = None,
        kream_brand: str | None = None,
        kream_product_name: str | None = None,
        kream_image_url: str | None = None,
        kream_all_text: str | None = None,
    ):
        obj = int.__new__(cls, int(value))
        obj.match_verified = match_verified
        obj.match_method = match_method
        obj.match_confidence = match_confidence
        obj.match_reason = match_reason
        obj.kream_brand = kream_brand
        obj.kream_product_name = kream_product_name
        obj.kream_image_url = kream_image_url
        obj.kream_all_text = kream_all_text
        return obj


# --- DB 및 CSV 관련 함수 ---

def find_product_in_db(serial_number: str) -> int | None:
    """SQLite DB에서 모델 번호로 상품을 검색합니다."""
    if not serial_number:
        return None
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("SELECT product_id FROM kream_products WHERE model_number = ?", (serial_number,))
        result = cur.fetchone()
        con.close()
        if result:
            product_id = result[0]
            print(f"[DB_CACHE] HIT: DB에서 품번 '{serial_number}'의 상품 ID '{product_id}'를 찾았습니다.", flush=True)
            return product_id
        else:
            print(f"[DB_CACHE] MISS: DB에서 품번 '{serial_number}'를 찾지 못했습니다. 웹 검색을 시도합니다.", flush=True)
            return None
    except sqlite3.Error as e:
        print(f"[DB_CACHE] ERROR: DB 조회 중 오류 발생 (품번: {serial_number}): {e}", flush=True)
        return None

def save_product_to_db(product_id: int, model_number: str, brand_name: str, product_name: str):
    """새 상품 정보를 DB에 저장합니다."""
    if not all([product_id, model_number]):
        return
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("""
            INSERT INTO kream_products (product_id, model_number, brand_name, product_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(product_id) DO NOTHING
        """, (product_id, model_number, brand_name or '', product_name or ''))
        con.commit()
        con.close()
        print(f"[DB_CACHE] SAVE: 새 상품 정보(ID: {product_id}, 품번: {model_number})를 DB에 저장했습니다.", flush=True)
    except sqlite3.Error as e:
        print(f"[DB_CACHE] ERROR: DB 저장 중 오류 발생: {e}", flush=True)


# --- 유틸리티 함수 ---
def _sleep_jitter(a=0.15, b=0.35):
    time.sleep(random.uniform(a, b))

def kst_ts(dt=None):
    # 예: 20260106144513+0900
    kst = timezone(timedelta(hours=9))
    dt = dt or datetime.now(kst)
    return dt.strftime("%Y%m%d%H%M%S%z")

from typing import Any

def _extract_text_from_item(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    te = item.get("text_element")
    if isinstance(te, dict):
        vars_ = te.get("variations")
        if isinstance(vars_, list) and vars_:
            v0 = vars_[0]
            if isinstance(v0, dict) and isinstance(v0.get("text"), str):
                return v0["text"]
        dv = te.get("default_variation")
        if isinstance(dv, dict) and isinstance(dv.get("text"), str):
            return dv["text"]
    if isinstance(item.get("text"), str):
        return item["text"]
    return None

def _parse_price_int(text: str | None) -> int | None:
    if not text:
        return None
    s = re.sub(r"[^\d]", "", text)
    return int(s) if s.isdigit() else None

def get_system_curl_path():
    """시스템의 curl 실행 파일 경로를 찾습니다. Windows System32 경로를 우선합니다."""
    if os.name == 'nt':
        # Windows System32에 있는 curl을 우선적으로 찾음 (PowerShell alias 등 회피)
        sys_curl = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "curl.exe")
        if os.path.exists(sys_curl):
            return sys_curl
    # 없을 경우 PATH에서 찾음
    return shutil.which("curl") or "curl"

def load_observed_headers():
    allowed = {"x-kream-api-version", "x-kream-web-build-version",
               "x-kream-web-request-secret", "x-kream-device-id", "user-agent"}
    try:
        with open(get_persistent_path("cookies_token.json"), encoding="utf-8") as f:
            saved = json.load(f).get("request_headers", {})
        return {k.lower(): str(v) for k, v in saved.items() if k.lower() in allowed}
    except (OSError, ValueError, AttributeError):
        return {}


def run_curl_json(url: str, header_lines: list[str], timeout_sec: int = 20) -> dict[str, Any]:
    from shop_comparator.shops.browser_transport import current
    from shop_comparator.core.crawl_errors import response_json
    browser = current("kream")
    if browser:
        headers = {line.split(":", 1)[0]: line.split(":", 1)[1].strip()
                   for line in header_lines if ":" in line}
        headers.update(load_observed_headers())
        return response_json(browser.get(url, headers=headers, timeout=timeout_sec), "KREAM")
    curl_exe = get_system_curl_path()
    cmd = [curl_exe, "-sS", "--compressed", "--max-time", str(timeout_sec), "-w", "\n%{http_code}", url]
    merged = {line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip()
              for line in header_lines if ":" in line}
    merged.update(load_observed_headers())
    for key, value in merged.items():
        cmd += ["-H", f"{key}: {value}"]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_sec,
            check=False,
        )
    except FileNotFoundError:
        raise RuntimeError(f"curl 실행 파일을 찾을 수 없습니다. (path: {curl_exe})")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"curl timeout ({timeout_sec}s)")

    if proc.returncode != 0:
        # 자세한 오류 로그를 위해 stderr 앞부분 포함
        err_msg = (proc.stderr or "").strip()
        if "try 'curl --help'" in err_msg:
             # curl 인자 파싱 에러인 경우 명령행 구조 문제일 수 있음
             raise RuntimeError(f"curl argument error: {err_msg[:500]}. Check headers or quoting.")
        raise RuntimeError(f"curl failed (code={proc.returncode}): {err_msg[:500]}")

    body, _, status = (proc.stdout or "").rpartition("\n")
    if not status.strip().isdigit():
        raise CrawlError("KREAM", "network", "HTTP 상태를 확인하지 못했습니다.")
    check_status("KREAM", int(status.strip()))
    body = body.strip()

    if body.lower().startswith("<!doctype html") or "<html" in body[:200].lower():
        # 차단됨
        with open("kream_error_curl.html", "w", encoding="utf-8") as f:
            f.write(body)
        raise RuntimeError("KREAM returned HTML via curl (blocked/error). Saved: kream_error_curl.html")

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise CrawlError("KREAM", "format", "API 응답이 JSON이 아닙니다.")


def to_cookiejar_from_selenium(cookies_list):
    jar = requests.cookies.RequestsCookieJar()
    if not cookies_list or not isinstance(cookies_list, list):
        return jar
    for c in cookies_list:
        cookie = requests.cookies.create_cookie(
            name=str(c.get("name","")),
            value=str(c.get("value","")),
            domain=c.get("domain"),
            path=c.get("path") or "/"
        )
        jar.set_cookie(cookie)
    return jar

# --- 토큰 관련 함수 (복원) ---
def _jwt_payload(tok: str) -> dict:
    try:
        parts = tok.split(".")
        if len(parts) != 3: return {}
        pad = "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except Exception:
        return {}

def _is_expired(tok: str, skew_sec=30) -> bool:
    p = _jwt_payload(tok)
    exp = p.get("exp")
    if not exp: return False
    if exp > 10**12: exp = exp // 1000
    now = int(time.time())
    return now >= (exp - skew_sec)

def _load_tokens(token_file=None):
    if token_file is None:
        token_file = get_persistent_path("cookies_token.json")
    try:
        with open(token_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        access = (data.get("access_token") or "").strip()
        refresh = (data.get("refresh_token") or "").strip()
        ua = (data.get("user_agent") or "").strip()
        device_id = (data.get("device_id") or "").strip()
        sentry_trace = (data.get("sentry_trace") or "").strip()
        baggage = (data.get("baggage") or "").strip()
        
        global SENTRY_TRACE, BAGGAGE
        SENTRY_TRACE = sentry_trace
        BAGGAGE = baggage
        
        return access, refresh, ua, device_id, sentry_trace, baggage
    except (json.JSONDecodeError, IOError) as e:
        print(f"[ERROR] 토큰 파일 로드 실패: {e}")
        return None, None, None, None, None, None

# --- KREAM API 헤더 빌더 ---
def _base_headers(access_token: str | None = None, api_version: str | None = None) -> dict:
    if api_version is None:
        api_version = KREAM_API_VERSION
    loaded_ua = None
    loaded_device_id = None
    
    # 1. 환경변수 우선
    env_bearer = os.getenv("KREAM_BEARER")
    if env_bearer:
        token_to_use = env_bearer
    else:
        # 2. 인자가 없으면 파일에서 로드 시도
        if not access_token:
            loaded_token, _, loaded_ua, loaded_device_id, _, _ = _load_tokens()
            token_to_use = loaded_token
        else:
            token_to_use = access_token
    
    # 토큰 정제 (따옴표 제거 등)
    if token_to_use:
        token_to_use = str(token_to_use).strip().strip('"').strip("'")
        # 디버깅용 로그 (토큰 확인)
        # print(f"[KREAM_DEBUG] Using Token: {token_to_use[:20]}...")

    h = {
        "User-Agent": loaded_ua or UA,
        "Accept": "*/*",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "X-Kream-DEVICE-ID": loaded_device_id or KREAM_DEVICE_ID,
        "X-KREAM-CLIENT-DATETIME": kst_ts(),
        "X-KREAM-API-VERSION": api_version,
        "X-KREAM-WEB-BUILD-VERSION": KREAM_WEB_BUILD_VER,
        "X-KREAM-WEB-REQUEST-SECRET": KREAM_WEB_REQUEST_SEC,
        "Origin": "https://kream.co.kr",
        "Connection": "keep-alive",
        # "Sec-Fetch-Dest": "empty",
        # "Sec-Fetch-Mode": "cors",
        # "Sec-Fetch-Site": "same-site",
    }
    
    # Sentry 관련은 일단 유지하되 필수 여부 확인 필요
    if SENTRY_TRACE and BAGGAGE:
        h["sentry-trace"] = SENTRY_TRACE
        h["baggage"] = BAGGAGE

    if token_to_use: 
        h['Authorization'] = f'Bearer {token_to_use}'
        
    return h

class KreamAuthException(Exception):
    """KREAM API 인증 실패 시 발생하는 예외"""
    pass

class AllProxiesFailedException(Exception):
    """모든 프록시가 실패했을 때 발생하는 예외"""
    pass

def make_request_with_proxy(method, url, **kwargs):
    """
    프록시를 사용하여 요청을 보내고, 레이트 리밋 시 프록시를 순환하며 재시도합니다.
    """
    if proxy_manager.get_proxy_count() == 0:
        # 프록시가 설정되지 않은 경우, 직접 요청
        # print("[Request] No proxies configured. Sending direct request.")
        return requests.request(method, url, **kwargs)

    max_retries = proxy_manager.get_proxy_count()
    for i in range(max_retries):
        proxy = proxy_manager.get_next_proxy()
        if not proxy:
            continue

        # print(f"[Proxy] Using proxy: {proxy} (Attempt {i+1}/{max_retries})")
        proxy_dict = {"http": proxy, "https": proxy}
        
        try:
            response = requests.request(method, url, proxies=proxy_dict, **kwargs)
            
            if response.status_code == 429:
                print(f"[Proxy] Rate limit (429) detected with proxy {proxy}. Trying next proxy.")
                _sleep_jitter(0.5, 1.0) # 다음 프록시 시도 전 잠시 대기
                continue
            
            # 401은 인증오류로, 프록시 문제가 아니므로 즉시 예외 발생
            if response.status_code == 401:
                raise KreamAuthException("쿠키/토큰 인증 실패 (401)")

            response.raise_for_status()
            return response

        except requests.exceptions.ProxyError as e:
            print(f"[Proxy] Proxy error with {proxy}: {e}. Trying next proxy.")
            _sleep_jitter(0.5, 1.0)
            continue
        except requests.exceptions.RequestException as e:
            # 다른 네트워크 관련 예외 처리
            print(f"[Proxy] Request failed with proxy {proxy}: {e}. Trying next proxy.")
            _sleep_jitter(0.5, 1.0)
            continue

    raise AllProxiesFailedException("All configured proxies failed.")


def build_kream_headers_for_product(product_id: int, api_version: str | None = None, access_token: str | None = None) -> dict:
    h = _base_headers(access_token=access_token, api_version=api_version)
    h["Referer"] = f"https://kream.co.kr/products/{product_id}"
    return h

def build_kream_headers_for_search(keyword: str) -> dict:
    h = _base_headers()
    h["Referer"] = f"https://kream.co.kr/search?keyword={quote(keyword, safe='')}&tab=products&footer=home"
    return h

def build_kream_headers_for_sales(product_id: int, access_token: str) -> dict:
    h = _base_headers(access_token)
    h["Referer"] = f"https://kream.co.kr/products/{product_id}?fetchRelated=true"
    return h

# --- KREAM API 호출 함수 ---

def _make_curlish_session() -> requests.Session:
    s = requests.Session()
    ad = requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0)
    s.mount("https://", ad); s.mount("http://", ad)
    return s

def get_sales_history(product_id: int, access_token: str, cookies):

    if not product_id or not access_token:

        print("[get_sales_history] product_id / access_token 필요"); return None

    params = {"cursor": "1", "per_page": "50", "sort": "date_created[desc]"}

    headers = build_kream_headers_for_sales(product_id, access_token)

    for attempt in range(4): 

        try:

            params["request_key"] = str(uuid.uuid4())

            url = f"https://api.kream.co.kr/api/p/products/{product_id}/sales?{urlencode(params, safe='[]')}"

            headers["X-KREAM-CLIENT-DATETIME"] = kst_ts()

            r = make_request_with_proxy('get', url, headers=headers, timeout=15)

            data = r.json()
            tot = int(data.get("total") or 0)
            if tot == 0 and attempt < 3:
                _sleep_jitter(0.25, 0.4)
                continue
            return data
        except (requests.RequestException, AllProxiesFailedException) as e:
            print(f"[get_sales_history] 요청 실패 (시도 {attempt+1}): {e}")
            if isinstance(e, AllProxiesFailedException):
                raise e
            _sleep_jitter(0.5 * (2 ** attempt), 0.8 * (2 ** attempt))
        except KreamAuthException as e:
            print(f"[get_sales_history] 401 Unauthorized. UA/Device/토큰 점검.")
            raise e

    print(f"[get_sales_history] 반복 재시도 후 빈 응답/오류."); return None

SESSION_TOKEN = None  # token of the live browser session (web app); None = anonymous


def set_session_token(token):
    global SESSION_TOKEN
    SESSION_TOKEN = token or None


def _search_once(sess: requests.Session, keyword: str) -> dict | None:
    from shop_comparator.shops.browser_transport import current
    loaded_ua = loaded_device_id = None
    if current("kream"):
        # Search is public on the site: never send a stale stored token here.
        token_to_use = SESSION_TOKEN
    else:
        access_token, _, loaded_ua, loaded_device_id, _, _ = _load_tokens()
        env_bearer = os.getenv("KREAM_BEARER")
        token_to_use = env_bearer if env_bearer else access_token
        if token_to_use:
            token_to_use = str(token_to_use).strip().strip('"').strip("'")

        if not token_to_use:
            print("[_search_once] 토큰이 없어 검색을 수행할 수 없습니다.")
            return None

    url = "https://api.kream.co.kr/api/screens/search/products"
    
    params = {
        "keyword": keyword,
        "tab": "products",
        "typed_string": keyword,
        "search_type": "direct",
        "request_key": str(uuid.uuid4()),
    }
    
    qs = urlencode(params)
    full_url = f"{url}?{qs}"
    
    dt = kst_ts()
    # Header construction for curl
    header_lines = [
        'accept: */*',
        'accept-language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
        *([f'authorization: Bearer {token_to_use}'] if token_to_use else []),
        'origin: https://kream.co.kr',
        f'referer: https://kream.co.kr/search?keyword={quote(keyword, safe="")}&tab=products&footer=home',
        'priority: u=1, i',
        'sec-ch-ua: "Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        'sec-ch-ua-mobile: ?0',
        'sec-ch-ua-platform: "Windows"',
        'sec-fetch-dest: empty',
        'sec-fetch-mode: cors',
        'sec-fetch-site: same-site',
        f'user-agent: {loaded_ua or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"}',
        f'x-kream-api-version: {KREAM_API_VERSION}',
        f'x-kream-client-datetime: {dt}',
        f'x-kream-device-id: {loaded_device_id or KREAM_DEVICE_ID}',
        f'x-kream-web-build-version: {KREAM_WEB_BUILD_VER}',
        f'x-kream-web-request-secret: {KREAM_WEB_REQUEST_SEC}',
    ]

    for attempt in range(4):
        try:
            data = run_curl_json(full_url, header_lines, timeout_sec=15)
            
            # 검색 결과 추출 테스트
            extracted = _extract_products_from_screen(data)
            
            content = (data or {}).get("content") or {}
            items = content.get("items") or []

            if not items and attempt < 3:
                _sleep_jitter(0.25, 0.4)
                continue
            
            return data

        except CrawlError:
            raise
        except Exception as e:
            print(f"[KREAM] 상품 검색 중 예외 발생 (시도 {attempt+1}): {e}")
            _sleep_jitter(0.5 * (2 ** attempt), 0.8 * (2 ** attempt))

    print(f"[KREAM] 상품 검색 최종 실패 (키워드: {keyword})")
    return None

def _extract_products_from_screen(data: dict) -> list[dict]:
    """
    KREAM search 응답(UI JSON)에서 product_card만 추출해서
    상품 핵심 정보를 리스트로 리턴 (kream_search.py 로직)
    """
    out = []
    content = (data or {}).get("content") or {}
    items = content.get("items") or []

    for it in items:
        if (it or {}).get("display_type") != "product_card":
            continue

        # product_id
        raw_id = (it.get("id") or "")
        product_id = raw_id.split("/")[-1] if "/" in raw_id else raw_id

        # 클릭 이벤트 properties JSON에 더 많은 정보가 있음
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
        text_values = []

        def walk(node):
            nonlocal brand, name_ko, price_text, image_url, text_values
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
                    text_values.append(t)
                    # 브랜드는 보통 "UGG" 같이 짧게 뜸
                    if not brand and len(t) <= 20 and t.isupper():
                        brand = t
                    # 상품명(한글)
                    if not name_ko and ("(" in t or "어그" in t or "블랙" in t or len(t) > 5):
                         # 브랜드가 이미 있고, 텍스트가 브랜드랑 다르면 이름일 가능성 높음
                        if brand and t != brand:
                            name_ko = t
                        elif not brand: # 브랜드 못찾았는데 긴 텍스트면 이름일 수도
                            name_ko = t

                    # 가격 (여기서는 단순 텍스트 추출용, 실제 로직엔 안쓰일수도)
                    if not price_text and ("원" in t and any(ch.isdigit() for ch in t)):
                        price_text = t

            # 하위 아이템 재귀
            for child in (node.get("items") or []):
                walk(child)
            for child in (node.get("image_item"), node.get("label1_item"), node.get("right_item"), node.get("description_item"), node.get("title_item")):
                if isinstance(child, dict):
                    walk(child)
        
        walk(it)
        
        out.append({
            "id": product_id,
            "brand": brand,
            "name_ko": name_ko,
            "price_text": price_text,
            "product_url": product_url,
            "image_url": image_url,
            "all_text": " ".join(dict.fromkeys(text_values)),
        })
    return out

def _verified_product_from_search(
    json_obj: dict,
    brand: str | None = None,
    serial_number: str | None = None,
    keyword: str | None = None,
    image_url: str | None = None,
):
    products = _extract_products_from_screen(json_obj)
    if not products:
        print("[KREAM_MATCH] search result is empty.", flush=True)
        return None

    decision = select_best_kream_match(
        {
            "name": keyword or "",
            "brand": brand or "",
            "serial_number": serial_number or "",
            "image_url": image_url or "",
        },
        products,
    )
    print(
        f"[KREAM_MATCH] matched={decision.matched}, confidence={decision.confidence:.2f}, "
        f"method={decision.method}, reason={decision.reason}",
        flush=True,
    )

    if not decision.matched or not decision.product:
        return None

    product = decision.product
    return {
        "release": {"id": product["id"]},
        "name_ko": product.get("name_ko"),
        "brand": {"name": product.get("brand")},
        "image_url": product.get("image_url"),
        "all_text": product.get("all_text"),
        "match_confidence": decision.confidence,
        "match_method": decision.method,
        "match_reason": decision.reason,
    }

def search_product(keyword: str, brand: str | None, serial_number: str | None, cookies, image_url: str | None = None):
    with open("kream_crawler_debug.log", "a", encoding="utf-8") as f:
        f.write(f"search_product 호출됨: 키워드='{keyword}', 브랜드='{brand}', 시리얼='{serial_number}'\n")

    trust_legacy_matches = os.getenv("KREAM_TRUST_EXISTING_MATCHES", "").strip().lower() in {"1", "true", "yes", "on"}
    if serial_number and trust_legacy_matches:
        product_id_from_db = find_product_in_db(serial_number)
        if product_id_from_db:
            return KreamSearchResult(
                product_id_from_db,
                match_verified=False,
                match_method="legacy_model_cache",
                match_confidence=None,
                match_reason="Trusted by KREAM_TRUST_EXISTING_MATCHES legacy model cache.",
            )

    # --- 이하 DB에 없는 경우 웹 검색 로직 ---
    print(f"\n[KREAM_SEARCH] 시작. 키워드: '{keyword}', 브랜드: '{brand}', 시리얼: '{serial_number}'", flush=True)
    sess = requests.Session()
    queries = []
    if serial_number: queries.append(serial_number.strip())
    if keyword: queries.append(keyword.strip())
    if serial_number:
        s = serial_number.strip()
        while len(s) >= 7:
            if s not in queries:
                queries.append(s)
            s = s[:-3]
    if keyword and "/" in keyword:
        if " / " in keyword: _, tail = keyword.split(" / ", 1)
        else: _, tail = keyword.split("/", 1)
        if tail.strip() and tail.strip() not in queries: queries.append(tail.strip())
    if keyword and "-" in keyword:
        if " - " in keyword: _, tail = keyword.split(" - ", 1)
        else: _, tail = keyword.split("-", 1)
        if tail.strip() and tail.strip() not in queries: queries.append(tail.strip())
    
    # print(f"[KREAM_SEARCH] 생성된 검색어: {queries}", flush=True)

    for q in queries:
        _sleep_jitter(1.5, 3.0) # 검색 간 딜레이 추가
        try:
            print(f"[KREAM_SEARCH] '{q}'로 검색 시도...", flush=True)
            with open("kream_last_search.txt", "w", encoding="utf-8") as f:
                f.write(q)
            data = _search_once(sess, q)
            
            p = _verified_product_from_search(data, brand, serial_number, keyword=keyword, image_url=image_url)
            if p:
                rel = p.get("release") or {}
                pid = rel.get("id")
                
                if not pid:
                    print(f"[KREAM_SEARCH] 필터링 결과, 상품 ID를 찾을 수 없음. 다음 검색어로 넘어갑니다.", flush=True)
                    continue

                print(f"[KREAM_SEARCH] 최종 상품 ID: {pid} 찾음. ('{p.get('name_ko')}')", flush=True)

                # 2. DB에 신규 데이터 저장
                if serial_number:
                    product_name_ko = p.get('name_ko', '')
                    brand_name = brand or (p.get("brand") or {}).get("name", '')
                    save_product_to_db(pid, serial_number, brand_name, product_name_ko)

                return KreamSearchResult(
                    pid,
                    match_verified=True,
                    match_method=p.get("match_method") or "verified_search",
                    match_confidence=p.get("match_confidence"),
                    match_reason=p.get("match_reason"),
                    kream_brand=(p.get("brand") or {}).get("name"),
                    kream_product_name=p.get("name_ko"),
                    kream_image_url=p.get("image_url"),
                    kream_all_text=p.get("all_text"),
                )
            else:
                # print(f"[KREAM_SEARCH] 필터링 결과, 일치하는 상품 없음. 다음 검색어로 넘어갑니다.", flush=True)
                continue

        except CrawlError:
            raise
        except Exception as e:
            print(f"[KREAM_SEARCH] 상품 검색 중 치명적 오류 발생 (쿼리: '{q}'): {e}", flush=True)
            continue
            
    print(f"[KREAM_SEARCH] 모든 검색어 시도 후에도 상품 ID를 찾지 못함.", flush=True)
    return None

def _extract_prices_recursive(payload: dict, action_values: tuple) -> list[dict]:
    """
    KREAM API v52+ 응답(UI Driven)에서 재귀적으로 가격 정보를 추출합니다.
    action_values: ('instant_buy', 'bid_buy') 등
    """
    results = []

    def walk(node):
        if isinstance(node, dict):
            actions = node.get("actions")
            if isinstance(actions, list):
                # 가격/설명은 node에 달려있음
                title_text = _extract_text_from_item(node.get("title_item"))
                price = _parse_price_int(title_text)
                desc_text = _extract_text_from_item(node.get("description_item"))

                for act in actions:
                    if not isinstance(act, dict) or act.get("type") != "named_action":
                        continue

                    action = act.get("value")
                    if action in {"buy", "sell"}:
                        from shop_comparator.shops.get_kream_price6 import extract_button_title_text
                        button_text = extract_button_title_text(node).lower()
                        wording = ("즉시 구매", "instant buy") if action == "buy" else ("즉시 판매", "instant sell")
                        if any(word in button_text for word in wording):
                            action = "instant_" + action
                    if action not in action_values:
                        continue

                    params = act.get("parameters") or {}
                    
                    def first(v):
                        if isinstance(v, list) and v:
                            return v[0]
                        return v

                    raw_price = first(params.get("price"))
                    if raw_price is not None:
                        price_text = str(raw_price).strip().replace(",", "")
                        action_price = int(price_text) if price_text.isdigit() else None
                    else:
                        action_price = price
                    results.append({
                        "action": action,
                        "option_key": first(params.get("option_key")),
                        "price": action_price,
                        "delivery_text": desc_text,
                    })

            for v in node.values():
                walk(v)

        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(payload)
    
    # 중복 제거: option_key 기준, price 있는 항목 우선
    uniq = {}
    for item in results:
        ok = item.get("option_key")
        if not ok: continue
        
        # (action, option_key)를 키로 하거나, 단순히 option_key를 키로 하여 최적가 선택
        key = (item.get("action"), ok)
        if key not in uniq:
            uniq[key] = item
        else:
            # 기존에 가격이 없는데 현재 항목에 있으면 업데이트
            old, new = uniq[key].get("price"), item.get("price")
            if new is not None and (old is None or
                    ("buy" in item["action"] and new < old) or
                    ("sell" in item["action"] and new > old)):
                uniq[key] = item
                
    return list(uniq.values())

def get_prices(product_id, cookies, picker_type="buy", access_token=None, strict=False):
    if not product_id:
        return None

    # Load token logic (same as get_prices)
    loaded_ua = None
    loaded_device_id = None
    if not access_token:
        access_token, _, loaded_ua, loaded_device_id, _, _ = _load_tokens()
    else:
        _, _, loaded_ua, loaded_device_id, _, _ = _load_tokens()
    env_bearer = os.getenv("KREAM_BEARER")
    token_to_use = env_bearer if env_bearer else access_token
    if token_to_use:
        token_to_use = str(token_to_use).strip().strip('"').strip("'")

    if not token_to_use:
        if strict:
            raise CrawlError("KREAM", "auth", "먼저 KREAM 로그인을 완료해 주세요.")
        return {}

    url = "https://api.kream.co.kr/api/p/options/display"
    qs = urlencode({
        "product_id": str(product_id),
        "picker_type": picker_type,
        "request_key": str(uuid.uuid4()),
    })
    full_url = f"{url}?{qs}"
    
    dt = kst_ts()
    header_lines = [
        'accept: */*',
        'accept-language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
        f'authorization: Bearer {token_to_use}',
        'origin: https://kream.co.kr',
        f'referer: https://kream.co.kr/products/{product_id}',
        'priority: u=1, i',
        'sec-ch-ua: "Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        'sec-ch-ua-mobile: ?0',
        'sec-ch-ua-platform: "Windows"',
        'sec-fetch-dest: empty',
        'sec-fetch-mode: cors',
        'sec-fetch-site: same-site',
        f'user-agent: {loaded_ua or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"}',
        f'x-kream-api-version: {KREAM_API_VERSION}',
        f'x-kream-client-datetime: {dt}',
        f'x-kream-device-id: {loaded_device_id or KREAM_DEVICE_ID}',
        f'x-kream-web-build-version: {KREAM_WEB_BUILD_VER}',
        f'x-kream-web-request-secret: {KREAM_WEB_REQUEST_SEC}',
    ]

    try:
        data = run_curl_json(full_url, header_lines, timeout_sec=12)

        # v52 파싱 로직 적용
        target_actions = ("instant_buy", "bid_buy") if picker_type == "buy" else ("instant_sell", "bid_sell")
        extracted = _extract_prices_recursive(data, target_actions)
        
        out = {}
        # 2. instant로 덮어쓰기 (우선순위 높음)
        for item in extracted:
            if "instant" in item["action"]:
                size = item.get("option_key")
                price = item.get("price")
                if size and price:
                    out[str(size)] = int(price)
                    
        return out
    except Exception:
        if strict:
            raise
        return {}

def get_prices2(product_id, cookies, picker_type="sell", access_token=None):
    # get_prices와 로직 동일하므로 그대로 호출 (wrapper)
    return get_prices(product_id, cookies, picker_type, access_token)

def _extract_all_sizes(a: dict) -> list[str]:
    # v52 응답에서 사이즈 목록 추출
    sizes = set()
    
    # 1. 즉시 구매 가능 사이즈
    buy_items = _extract_prices_recursive(a, "instant_buy")
    for item in buy_items:
        if item.get("option_key"):
            sizes.add(str(item.get("option_key")))
            
    # 2. 즉시 판매 가능 사이즈
    sell_items = _extract_prices_recursive(a, "instant_sell")
    for item in sell_items:
        if item.get("option_key"):
            sizes.add(str(item.get("option_key")))
    
    # 3. 기타 액션
    other_actions = ["bid", "ask", "instant_buy", "instant_sell"]
    def walk_for_any_option_key(node):
        if isinstance(node, dict):
            actions = node.get("actions")
            if isinstance(actions, list):
                for act in actions:
                    if isinstance(act, dict) and act.get("type") == "named_action":
                        params = act.get("parameters") or {}
                        opt = params.get("option_key")
                        if isinstance(opt, list) and opt: opt = opt[0]
                        if opt:
                            sizes.add(str(opt))
            for v in node.values():
                walk_for_any_option_key(v)
        elif isinstance(node, list):
            for x in node:
                walk_for_any_option_key(x)

    walk_for_any_option_key(a)

    return sorted(list(sizes))

def get_all_sizes(product_id, cookies, access_token=None):
    if not product_id:
        return []

    # Load token logic (same as get_prices)
    loaded_ua = None
    loaded_device_id = None
    if not access_token:
        access_token, _, loaded_ua, loaded_device_id, _, _ = _load_tokens()
    else:
        _, _, loaded_ua, loaded_device_id, _, _ = _load_tokens()
    env_bearer = os.getenv("KREAM_BEARER")
    token_to_use = env_bearer if env_bearer else access_token
    if token_to_use:
        token_to_use = str(token_to_use).strip().strip('"').strip("'")

    if not token_to_use:
        return []

    url = "https://api.kream.co.kr/api/p/options/display"
    qs = urlencode({
        "product_id": str(product_id),
        "picker_type": "buy",
        "request_key": str(uuid.uuid4()),
    })
    full_url = f"{url}?{qs}"
    
    dt = kst_ts()
    header_lines = [
        'accept: */*',
        'accept-language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
        f'authorization: Bearer {token_to_use}',
        'origin: https://kream.co.kr',
        f'referer: https://kream.co.kr/products/{product_id}',
        'priority: u=1, i',
        'sec-ch-ua: "Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        'sec-ch-ua-mobile: ?0',
        'sec-ch-ua-platform: "Windows"',
        'sec-fetch-dest: empty',
        'sec-fetch-mode: cors',
        'sec-fetch-site: same-site',
        f'user-agent: {loaded_ua or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"}',
        f'x-kream-api-version: {KREAM_API_VERSION}',
        f'x-kream-client-datetime: {dt}',
        f'x-kream-device-id: {loaded_device_id or KREAM_DEVICE_ID}',
        f'x-kream-web-build-version: {KREAM_WEB_BUILD_VER}',
        f'x-kream-web-request-secret: {KREAM_WEB_REQUEST_SEC}',
    ]

    try:
        data = run_curl_json(full_url, header_lines, timeout_sec=12)
        sizes = _extract_all_sizes(data)
        return sizes
    except Exception as e:
        print(f"[get_all_sizes] curl 실행 중 오류: {e}")
        return []

if __name__ == '__main__':
    pass

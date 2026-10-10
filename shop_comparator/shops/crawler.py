# -*- coding: utf-8 -*-

from __future__ import annotations

import os, re, time, json, math, threading, subprocess, sys
import requests
from typing import Dict, List, Any, Tuple, Optional

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoAlertPresentException, UnexpectedAlertPresentException, TimeoutException
from selenium.webdriver.common.keys import Keys

# 모듈화된 파일 임포트
from shop_comparator.core import config
from shop_comparator.core.crawl_errors import CrawlError, response_json

DEBUG_HTTP = os.getenv("SHOP_DEBUG_HTTP", "").strip().lower() in {"1", "true", "yes", "on"}


def _debug(*args, **kwargs):
    if DEBUG_HTTP:
        print(*args, **kwargs)


def _log_api_call(func_name, url, params=None, data=None, headers=None, response=None):
    """Helper function to log API call details (only with SHOP_DEBUG_HTTP=1)."""
    if not DEBUG_HTTP:
        return
    print(f"\n--- API CALL: {func_name} ---")
    print(f"URL: {url}")
    if params:
        print(f"PARAMS: {params}")
    if data:
        # For JSON data, pretty-print it.
        if isinstance(data, dict) or isinstance(data, list):
            print(f"DATA (JSON): {json.dumps(data, ensure_ascii=False, indent=2)}")
        else:
            print(f"DATA: {data}")
            
    if headers:
        # Print only relevant headers to avoid excessive logging
        relevant_headers = {k: v for k, v in headers.items() if k in ['Content-Type', 'Referer']}
        print(f"HEADERS: {relevant_headers}")

    if response is not None:
        print(f"STATUS: {response.status_code}")
    print(f"--- END API CALL ---\n")


# ---------- 세션 빌드 ----------
def build_session(cookie_header: str, auth_payload: Optional[dict] = None) -> requests.Session:
    from shop_comparator.shops.browser_transport import current
    s = current("musinsa") or requests.Session()
    s.headers.update(_prepare_request_headers({
        "User-Agent": config.UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        "Origin": "https://www.musinsa.com",
        "Referer": config.RANKING_PAGE,
    }, cookie_header, auth_payload))
    try:
        s.get(config.RANKING_PAGE, timeout=10)
    except Exception:
        pass
    return s


def _http():
    from shop_comparator.shops.browser_transport import current
    return current("musinsa") or requests

# ---------- 쿠키 얻기 (브라우저 자동) ----------
TARGET_COOKIES = {"app_atk", "app_rtk", "mss_last_login", "mss_mac", "_gf"}
AUTH_STORAGE_KEYS = {"app_atk", "app_rtk", "mss_last_login", "mss_mac"}
AUTH_ORIGINS = [
    "https://www.musinsa.com/",
    "https://member.one.musinsa.com/",
]
_AUTH_PAYLOAD = {}

def set_auth_payload(auth_payload: Optional[dict]):
    global _AUTH_PAYLOAD
    _AUTH_PAYLOAD = auth_payload or {}

def _get_all_cookies(driver):
    try:
        result = driver.execute_cdp_cmd("Network.getAllCookies", {})
        cookies = result.get("cookies") or []
        if cookies:
            return [cookie for cookie in cookies if
                    str(cookie.get("domain", "")).lstrip(".") == "musinsa.com" or
                    str(cookie.get("domain", "")).endswith(".musinsa.com")]
    except Exception:
        pass
    return driver.get_cookies()

def _build_cookie_header(cookies):
    seen = set()
    parts = []
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None or name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={value}")
    return "; ".join(parts)

def _get_web_storage(driver, storage_name):
    script = """
        const storage = window[arguments[0]];
        const out = {};
        for (let i = 0; i < storage.length; i += 1) {
            const key = storage.key(i);
            out[key] = storage.getItem(key);
        }
        return out;
    """
    try:
        return driver.execute_script(script, storage_name) or {}
    except Exception:
        return {}

def _extract_auth_values(auth_payload: Optional[dict] = None) -> dict:
    payload = auth_payload or _AUTH_PAYLOAD or {}
    values = {}

    for key in AUTH_STORAGE_KEYS:
        value = payload.get(key)
        if value:
            values[key] = str(value)

    for origin_data in (payload.get("origins") or {}).values():
        for storage_name in ("localStorage", "sessionStorage"):
            storage = origin_data.get(storage_name) or {}
            for key in AUTH_STORAGE_KEYS:
                value = storage.get(key)
                if value and key not in values:
                    values[key] = str(value)
    for cookie in payload.get("cookies") or []:
        if cookie.get("name") in AUTH_STORAGE_KEYS and cookie.get("value"):
            values.setdefault(cookie["name"], str(cookie["value"]))
    return values

def _merge_cookie_header(cookie_header: str, auth_payload: Optional[dict] = None) -> str:
    base_parts = []
    seen = set()
    for part in (cookie_header or "").split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        base_parts.append(f"{name}={value.strip()}")

    for name, value in _extract_auth_values(auth_payload).items():
        if name in seen or value is None:
            continue
        seen.add(name)
        base_parts.append(f"{name}={value}")

    return "; ".join(base_parts)

def _extract_access_token(auth_payload: Optional[dict] = None) -> str:
    token = _extract_auth_values(auth_payload).get("app_atk", "")
    return token.strip().strip('"').strip("'")

def _prepare_request_headers(base_headers: dict, cookie_header: str, auth_payload: Optional[dict] = None) -> dict:
    headers = dict(base_headers)
    saved_ua = (auth_payload or _AUTH_PAYLOAD or {}).get("user_agent")
    if saved_ua:
        headers["User-Agent"] = saved_ua
    merged_cookie = _merge_cookie_header(cookie_header, auth_payload)
    if merged_cookie:
        headers["Cookie"] = merged_cookie
    token = _extract_access_token(auth_payload)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers

def _collect_current_auth_state(driver) -> dict:
    local_storage = _get_web_storage(driver, "localStorage")
    session_storage = _get_web_storage(driver, "sessionStorage")
    return {
        "url": driver.current_url,
        "localStorage": local_storage,
        "sessionStorage": session_storage,
    }

def _collect_auth_payload(driver) -> dict:
    payload = {
        "origins": {},
        "cookies": _get_all_cookies(driver),
    }
    for origin in AUTH_ORIGINS:
        try:
            driver.get(origin)
            time.sleep(1)
            payload["origins"][origin] = _collect_current_auth_state(driver)
        except Exception:
            payload["origins"][origin] = {
                "url": origin,
                "localStorage": {},
                "sessionStorage": {},
            }

    payload["cookies"] = _get_all_cookies(driver)
    auth_values = _extract_auth_values(payload)
    payload.update(auth_values)
    return payload

def _wait_login_outcome(driver, total_timeout=120, poll=0.5):
    end = time.time() + total_timeout
    while time.time() < end:
        try:
            alert = driver.switch_to.alert
            text = alert.text
            try: alert.accept()
            except Exception: pass
            return {"ok": False, "reason": "alert", "message": text}
        except NoAlertPresentException:
            pass

        try:
            cookies = _get_all_cookies(driver)
            names = {c.get("name") for c in cookies}
            if any(c.get("name") == "app_atk" and c.get("value") for c in cookies):
                return {"ok": True}

            storage_state = _collect_current_auth_state(driver)
            storage_keys = set(storage_state["localStorage"]) | set(storage_state["sessionStorage"])
            if any(storage_state[k].get("app_atk") for k in ("localStorage", "sessionStorage")):
                return {"ok": True}
        except UnexpectedAlertPresentException:
            try:
                alert = driver.switch_to.alert
                text = alert.text
                try: alert.accept()
                except Exception: pass
                return {"ok": False, "reason": "alert", "message": text}
            except NoAlertPresentException:
                pass
        time.sleep(poll)
    return {"ok": False, "reason": "timeout", "message": "로그인 결과 타임아웃"}

def get_cookie_with_selenium(uid: str, pwd: str, log_callback=None) -> dict:
    def log(msg):
        if log_callback: log_callback(msg)

    driver = None
    try:
        log("[INFO] 브라우저 실행…")
        options = webdriver.ChromeOptions()
        options.add_argument("--start-maximized")
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
        driver.get("https://www.musinsa.com/auth/login")

        log("[INFO] 로그인 페이지 로드 대기…")
        w = WebDriverWait(driver, 60)
        id_input = w.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[title='통합계정 또는 이메일']")))
        pw_input = w.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[title='비밀번호 입력']")))
        id_input.clear(); id_input.send_keys(uid)
        pw_input.clear(); pw_input.send_keys(pwd)
        log("[INFO] 아이디/비밀번호 입력 완료")
        time.sleep(1)
        pw_input.send_keys(Keys.RETURN)
        log("[INFO] 엔터 키 입력으로 로그인 시도")

        log("[INFO] 로그인 결과 대기(최대 120초)…")
        res = _wait_login_outcome(driver, total_timeout=120)
        if res.get("ok"):
            auth_payload = _collect_auth_payload(driver)
            cookie_header = _merge_cookie_header(_build_cookie_header(auth_payload.get("cookies") or []), auth_payload)
            token_found = bool(_extract_access_token(auth_payload))
            storage_summary = {
                origin: {
                    "localStorage": sorted((state.get("localStorage") or {}).keys()),
                    "sessionStorage": sorted((state.get("sessionStorage") or {}).keys()),
                }
                for origin, state in (auth_payload.get("origins") or {}).items()
            }
            res["cookie_header"] = cookie_header
            res["auth_payload"] = auth_payload
            log(f"[INFO] 저장소 키 감지: {json.dumps(storage_summary, ensure_ascii=False)}")
            log(f"[INFO] app_atk 감지: {'Y' if token_found else 'N'}")
            log("[OK] 쿠키/인증 정보 추출 성공.")
            try:
                driver.quit()
                log("[INFO] 브라우저 종료")
            except Exception:
                pass
        else:
            reason = res.get("reason")
            msg = res.get("message", "")
            if reason == "alert":
                log(f"[ERROR] 로그인 실패(알럿): {msg}")
            elif reason == "timeout":
                log("[WARN] 로그인 타임아웃. 브라우저에서 직접 완료해 주세요.")
            else:
                log("[ERROR] 알 수 없는 실패. 브라우저를 열어둡니다.")
        return res

    except Exception as e:
        log(f"[EXCEPT] {e}")
        return {"ok": False, "reason": "exception", "message": str(e)}

# ---------- 랭킹 / 상세 ----------
def _pick_rank_items_from_layout(obj) -> List[Tuple[int, str, str]]:
    out = []
    def walk(x):
        if isinstance(x, dict):
            img = x.get("image")
            if isinstance(img, dict) and isinstance(img.get("rank"), int):
                rank = img["rank"]
                link = None
                oc = x.get("onClick")
                if isinstance(oc, dict):
                    link = oc.get("url")
                if not link:
                    for v in x.values():
                        if isinstance(v, str) and v.startswith("https://www.musinsa.com/products/"):
                            link = v; break
                if link:
                    m = re.search(r"/products/(\d+)", link)
                    if m:
                        pid = m.group(1)
                        out.append((rank, pid, link))
            for v in x.values():
                if isinstance(v, (dict, list)): walk(v)
        elif isinstance(x, list):
            for v in x: walk(v)
    walk(obj)
    out.sort(key=lambda t: t[0])
    seen, uniq = set(), []
    for rnk, pid, link in out:
        if pid not in seen:
            seen.add(pid); uniq.append((rnk, pid, link))
    return uniq

def fetch_ranking_top_n(sess: requests.Session, top_n: int) -> List[Tuple[int,str,str]]:
    # This function is part of ranking crawl, logging is in run_ranking_crawl
    j = sess.get(config.RANKING_LAYOUT, timeout=15).json()
    return _pick_rank_items_from_layout(j)[:top_n]

def fetch_details(sess: requests.Session, ids: List[str], chunk=80, sleep=0.5) -> Dict[str, dict]:
    details = {}
    for i in range(0, len(ids), chunk):
        part = ids[i:i+chunk]
        url = f"{config.DETAILS_BASE}?goodsNoList={','.join(part)}&saleStateList=SALE,SOLD_OUT"
        
        _log_api_call("fetch_details", url, headers=sess.headers)
        r = sess.get(url, timeout=15)
        _log_api_call("fetch_details", url, headers=sess.headers, response=r)

        data = response_json(r)
        for row in (data.get("data") or {}).get("list", []):
            gid = str(row.get("goodsNo"))
            if gid: details[gid] = row
        time.sleep(sleep)
    return details

def fetch_purchase_limit_info(sess: requests.Session, goods_no: str) -> Optional[Dict[str, Any]]:
    """
    Fetches purchase limit information for a single product.
    """
    url = f"{config.DETAILS_BASE}?goodsNoList={goods_no}&saleStateList=SALE,SOLD_OUT"
    
    _log_api_call("fetch_purchase_limit_info", url, headers=sess.headers)
    r = sess.get(url, timeout=15)
    _log_api_call("fetch_purchase_limit_info", url, headers=sess.headers, response=r)

    data = response_json(r)
    
    if data.get("data") and data["data"].get("list"):
        product_data = data["data"]["list"][0]
        # Extract the relevant fields based on user's example
        limit_info = {
            "isLimitedQuantity": product_data.get("isLimitedQuantity"),
            "isLimitedTotalQuantity": product_data.get("isLimitedTotalQuantity"),
            "limitedMaxQuantity": product_data.get("limitedMaxQuantity"),
            "limitedMinQuantity": product_data.get("limitedMinQuantity"),
            "isBuyForMember": product_data.get("isBuyForMember")
        }
        # Also return the product name and brand for the GUI
        limit_info["goodsName"] = product_data.get("goodsName")
        limit_info["brandName"] = product_data.get("brandName")
        limit_info["detail_meta"] = product_data # for later use if needed
        return limit_info
    return None

# ---------- 옵션 / 카트 / 주문 / 쿠폰 ----------
def fetch_options(sess: requests.Session, goods_no: int) -> Tuple[Dict[int, Dict[str, Any]], List[int]]:
    """
    options API에서 variantId -> {size, managedCode ...} 매핑 및 optionValueNos 추출
    URL: /{goodsNo}/options?goodsSaleType=SALE&optKindCd=SHOES
    """
    url = f"https://goods-detail.musinsa.com/api2/goods/{goods_no}/options"
    params = {"goodsSaleType": "SALE", "optKindCd": "SHOES"}
    
    _log_api_call("fetch_options", url, params=params, headers=sess.headers)
    r = sess.get(url, params=params, timeout=15)
    _log_api_call("fetch_options", url, params=params, headers=sess.headers, response=r)
    
    j = response_json(r)
    _debug("--- fetch_options API response ---")
    _debug(json.dumps(j, indent=2, ensure_ascii=False))
    _debug("---------------------------------")
    if not isinstance(j.get("data"), dict) or "optionItems" not in j["data"]:
        raise CrawlError("무신사", "schema", "상품 옵션 응답 형식이 변경되었습니다.")
    items = j["data"]["optionItems"]
    mapping: Dict[int, Dict[str, Any]] = {}
    option_value_nos: List[int] = []
    for it in items:
        variant_id = it.get("no")  # == productVariantId
        size = it.get("managedCode") or (it.get("optionValues") or [{}])[0].get("name")
        mapping[variant_id] = {
            "size": str(size),
            "goodsNo": it.get("goodsNo"),
            "raw": it,
        }
        for ovn in it.get("optionValueNos") or []:
            option_value_nos.append(int(ovn))
    return mapping, list(set(option_value_nos))

def fetch_inventories(sess: requests.Session, goods_no: int, option_value_nos: List[int]) -> Dict[int, Dict[str, Any]]:
    """
    inventories API에서 variantId -> {outOfStock, ETA...} 매핑 추출 (POST 방식)
    URL: /{goodsNo}/options/prioritized-inventories
    """
    url = f"https://goods-detail.musinsa.com/api2/goods/{goods_no}/options/prioritized-inventories"
    payload = {"optionValueNos": option_value_nos}
    
    # Update headers for POST request
    post_headers = {**sess.headers, "Content-Type": "application/json"}

    _log_api_call("fetch_inventories (POST)", url, data=payload, headers=post_headers)
    r = sess.post(url, headers=post_headers, json=payload, timeout=20)
    _log_api_call("fetch_inventories (POST)", url, data=payload, headers=post_headers, response=r)
    
    j = response_json(r)
    _debug("--- fetch_inventories API response ---")
    _debug(json.dumps(j, indent=2, ensure_ascii=False))
    _debug("-----------------------------------")
    rows = j.get("data", [])
    mapping: Dict[int, Dict[str, Any]] = {}
    for r_ in rows:
        vid = r_.get("productVariantId")
        mapping[vid] = r_
    return mapping

def fetch_options_any(sess: requests.Session, goods_no: str) -> dict:
    sess.headers["Referer"] = config.PRODUCT_URL.format(goods_no=goods_no)
    try:
        opt_map, option_value_nos = fetch_options(sess, int(goods_no))
        
        # If there are options, fetch their inventory status
        inv_map = {}
        if option_value_nos:
            inv_map = fetch_inventories(sess, int(goods_no), option_value_nos)

        # Combine the information
        combined_items = []
        for vid, meta in opt_map.items():
            inv = inv_map.get(vid, {})
            # Start with the raw option data
            new_item = meta['raw'].copy()
            
            # Update stock information from the inventory map
            if 'outOfStock' not in inv:
                raise CrawlError("무신사", "schema", "사이즈 재고를 확인하지 못했습니다.")
            new_item['outOfStock'] = inv['outOfStock']
            
            # isSoldOut is what the rest of the script uses
            new_item['isSoldOut'] = new_item['outOfStock']
            new_item['remainQuantity'] = inv.get('remainQuantity')
            
            # Add ETA information
            new_item['eta'] = (inv.get("deliveryExpectedArrival") or {}).get("expectedDate")
            
            combined_items.append(new_item)
        
        # Reconstruct the JSON structure that the rest of the application expects
        return {
            "meta": {"result": "SUCCESS"},
            "data": {
                "optionItems": combined_items,
            }
        }

    except Exception as e:
        print(f"[ERROR] goods_no {goods_no}의 옵션/재고 정보 결합 실패: {e}")
        raise

def choose_sellable_option(opts_json: dict) -> Tuple[str,str,dict] | None:
    items = ((opts_json.get("data") or {}).get("optionItems") or [])
    for it in items:
        if it.get("isDeleted") or it.get("isRedirect"): continue
        if it.get("isSoldOut") or it.get("outOfStock"): continue
        managed = str(it.get("managedCode") or "")
        gon = str(it.get("no") or "")
        if managed and gon:
            return (managed, gon, it)
    return None

def get_all_sellable_options(opts_json: dict) -> List[Tuple[str, str, dict]]:
    options = []
    items = ((opts_json.get("data") or {}).get("optionItems") or [])
    for it in items:
        if it.get("isDeleted") or it.get("isRedirect"): continue
        if it.get("isSoldOut") or it.get("outOfStock"): continue
        
        managed = str(it.get("managedCode") or "")
        gon = str(it.get("no") or "")
        if managed and gon:
            options.append((managed, gon, it))
    return options


def extract_available_sizes(opts_json: dict) -> List[str]:
    sizes = set()
    basics = ((opts_json.get("data") or {}).get("basic") or [])
    size_basic_nos = {str(b.get("no")) for b in basics if b.get("standardOptionNo")==6 or "사이즈" in (b.get("name") or "") or "size" in (b.get("name") or "").lower()}
    for it in ((opts_json.get("data") or {}).get("optionItems") or []):
        if it.get("isDeleted") or it.get("isRedirect"): continue
        if it.get("isSoldOut") or it.get("outOfStock"): continue
        
        # choose_sellable_option와 기준을 통일하기 위해 추가
        managed = str(it.get("managedCode") or "")
        gon = str(it.get("no") or "")
        if not (managed and gon):
            continue

        code = None
        for ov in (it.get("optionValues") or []):
            if str(ov.get("optionNo")) in size_basic_nos:
                code = ov.get("code") or ov.get("name")
                break
        if not code:
            if "^" in managed: code = managed.split("^")[-1]
            else: code = managed
        if code: sizes.add(str(code))
    try:
        return sorted(sizes, key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else x))
    except Exception:
        return sorted(sizes)

def extract_stock_size_sets(opts_json: dict) -> Dict[str, List[str]]:
    available_sizes = set()
    unavailable_sizes = set()
    basics = ((opts_json.get("data") or {}).get("basic") or [])
    size_basic_nos = {
        str(b.get("no"))
        for b in basics
        if b.get("standardOptionNo") == 6
        or "사이즈" in (b.get("name") or "")
        or "size" in (b.get("name") or "").lower()
    }

    for it in ((opts_json.get("data") or {}).get("optionItems") or []):
        if it.get("isDeleted") or it.get("isRedirect"):
            continue

        managed = str(it.get("managedCode") or "")
        gon = str(it.get("no") or "")
        if not (managed and gon):
            continue

        code = None
        for ov in (it.get("optionValues") or []):
            if str(ov.get("optionNo")) in size_basic_nos:
                code = ov.get("code") or ov.get("name")
                break
        if not code:
            code = managed.split("^")[-1] if "^" in managed else managed
        if not code:
            continue

        target = unavailable_sizes if (it.get("isSoldOut") or it.get("outOfStock")) else available_sizes
        target.add(str(code))

    def _sort(values):
        try:
            return sorted(values, key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else x))
        except Exception:
            return sorted(values)

    return {
        "available": _sort(available_sizes),
        "unavailable": _sort(unavailable_sizes),
    }

def add_to_cart_raw(cookie_header: str, goods_no: str, goods_option_no: str, auth_payload: Optional[dict] = None) -> dict:
    url = config.CART_SAVE_URL # Use the new URL from config
    headers = _prepare_request_headers({
        "User-Agent": config.UA,
        "Accept": "application/json",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Content-Type": "application/json",
        "Origin": "https://www.musinsa.com",
        "Referer": config.PRODUCT_URL.format(goods_no=goods_no),
    }, cookie_header, auth_payload)
    
    # New JSON payload structure
    data = {
        "origin": "PRODUCT",
        "isOrder": True,
        "goodsNo": int(goods_no),
        "options": [
            {
                "goodsOptionNo": int(goods_option_no),
                "quantity": 1,
                "addOptions": []
            }
        ]
    }
    
    _log_api_call("add_to_cart_raw", url, data=data, headers=headers)
    # Use json=data to automatically set Content-Type and serialize
    r = _http().post(url, headers=headers, json=data, timeout=15)
    _log_api_call("add_to_cart_raw", url, data=data, headers=headers, response=r)

    return response_json(r)

def fetch_order_form(cookie_header: str, auth_payload: Optional[dict] = None) -> dict:
    headers = _prepare_request_headers({
        "User-Agent": config.UA, "Accept": "application/json",
        "Accept-Language": "ko-KR,ko;q=0.8,en-US;q=0.5",
        "Origin": "https://www.musinsa.com",
        "Referer": "https://www.musinsa.com/",
    }, cookie_header, auth_payload)
    
    _log_api_call("fetch_order_form", config.ORDER_FORM_URL, headers=headers)
    r = _http().get(config.ORDER_FORM_URL, headers=headers, timeout=15)
    _log_api_call("fetch_order_form", config.ORDER_FORM_URL, headers=headers, response=r)

    j = response_json(r)
    if j.get("meta",{}).get("result") != "SUCCESS":
        raise CrawlError("무신사", "auth", "주문서를 확인할 수 없습니다. 다시 로그인해 주세요.")
    return j["data"]

def fetch_goods_coupons(cookie_header: str, goods_no: str, sale_price: int, brand_id: str, company_id: str, coupon_discount_flag: bool, auth_payload: Optional[dict] = None) -> List[dict]:
    headers = _prepare_request_headers({
        "User-Agent": config.UA, "Accept":"application/json",
        "Accept-Language":"ko-KR,ko;q=0.8,en-US;q=0.5",
        "Origin":"https://www.musinsa.com",
        "Referer":"https://www.musinsa.com/",
    }, cookie_header, auth_payload)
    params = {
        "goodsNo": str(goods_no),
        "usedCouponIdxList": "",
        "couponDiscount": str(coupon_discount_flag).lower(),
        "fullSalePrice": str(sale_price),
        "salePrice": str(sale_price),
        "brandId": brand_id,
        "companyId": company_id,
    }

    _log_api_call("fetch_goods_coupons", config.GOODS_COUPON_URL, params=params, headers=headers)
    r = _http().get(config.GOODS_COUPON_URL, params=params, headers=headers, timeout=15)
    _log_api_call("fetch_goods_coupons", config.GOODS_COUPON_URL, params=params, headers=headers, response=r)

    j = response_json(r)
    if j.get("meta",{}).get("result") != "SUCCESS":
        raise CrawlError("무신사", "api", "회원 쿠폰을 확인하지 못했습니다.")
    if not isinstance(j.get("data"), dict) or "list" not in j["data"]:
        raise CrawlError("무신사", "schema", "쿠폰 응답 형식이 변경되었습니다.")
    return j["data"]["list"]

def pick_best_coupon(coupons: List[dict]) -> dict | None:
    if not coupons: return None
    return max(coupons, key=lambda c: int(c.get("salePrice",0) or 0))

def compute_final(sale_price: int, coupon_discount: int, flags: dict) -> dict:
    after_coupon = max(0, sale_price - coupon_discount)
    combined = 0.0
    if flags.get("isMemberDiscount"):
        combined += float(flags.get("memberDiscountRate",0.0) or 0.0)
    # if flags.get("isUsePointDiscount"): # 멤버 등급 적립금은 최종 가격에 반영하지 않도록 수정
    #     combined += float(flags.get("memberSavePointRate",0.0) or 0.0)
    member_disc = math.floor(after_coupon * (combined/100.0))
    after_member = max(0, after_coupon - member_disc)
    final_price = after_member # 추가 적립금은 최종 가격에 반영하지 않도록 수정

    return {
        "after_coupon": after_coupon,
        "member_combined_rate": combined,
        "member_discount_amount": member_disc,
        "final_price": final_price,
    }


def _floor_to_ten(amount: float) -> int:
    return int(math.floor((amount or 0) / 10.0) * 10)


def compute_final_v2(sale_price: int, coupon_discount: int, flags: dict, member_info: Optional[dict] = None) -> dict:
    member_info = member_info or {}
    after_coupon = max(0, sale_price - coupon_discount)

    member_rate = float(flags.get("memberDiscountRate", 0.0) or 0.0) if flags.get("isMemberDiscount") else 0.0
    member_disc = _floor_to_ten(after_coupon * (member_rate / 100.0))

    point_discount = 0
    point_ratio = float(member_info.get("maximumUsePointRatio", 0) or 0)
    total_point = int(member_info.get("totalPoint", 0) or 0)
    point_cap = _floor_to_ten(after_coupon * (point_ratio / 100.0)) if point_ratio > 0 else 0
    point_rule = "disabled"
    goods_save_point = int(flags.get("goodsSavePoint", 0) or 0)
    point_member_rate = float(flags.get("memberSavePointRate", 0.0) or 0.0)

    if flags.get("isUsePointDiscount"):
        # Pre-point discount matches the expected point benefit for the item:
        # base goods save point + member-grade extra point on the post-coupon,
        # post-member-discount amount.
        point_discount = goods_save_point
        if point_member_rate > 0:
            point_discount += _floor_to_ten(max(0, after_coupon - member_disc) * (point_member_rate / 100.0))
        if point_discount > 0:
            point_rule = "goods_save_plus_member_rate"
        elif point_cap > 0 and total_point > 0:
            point_discount = max(0, min(total_point, point_cap))
            point_rule = "member_cap_fallback"

    final_price = max(0, after_coupon - member_disc - point_discount)

    return {
        "after_coupon": after_coupon,
        "member_combined_rate": member_rate,
        "member_discount_amount": member_disc,
        "point_discount_amount": point_discount,
        "point_discount_ratio": point_ratio,
        "point_discount_cap": point_cap,
        "point_discount_rule": point_rule,
        "final_price": final_price,
    }

# ---------- 결제 프로모션 ----------
_PAYMENT_EVENT_CACHE = None
_PROMO_RE = re.compile(r"(\d+)\s*(만원|천원|원)\s*이상.*?(\d+)\s*(만원|천원|원)\s*할인")

def _to_won(num: int, unit: str) -> int:
    unit = (unit or "").strip()
    if unit.startswith("만"): return int(num) * 10000
    if unit.startswith("천"): return int(num) * 1000
    return int(num)

def _parse_rule_line(text: str):
    t = text.replace(" ", "")
    m = _PROMO_RE.search(t)
    if not m: return None
    th_num, th_unit, disc_num, disc_unit = m.group(1), m.group(2), m.group(3), m.group(4)
    return {
        "threshold": _to_won(int(th_num), th_unit),
        "discount":  _to_won(int(disc_num), disc_unit),
        "raw": text,
    }

def fetch_payment_events(cookie_header: str, auth_payload: Optional[dict] = None) -> dict:
    global _PAYMENT_EVENT_CACHE
    if _PAYMENT_EVENT_CACHE is not None:
        return _PAYMENT_EVENT_CACHE
    headers = _prepare_request_headers({
        "User-Agent": config.UA, "Accept":"application/json",
        "Accept-Language":"ko-KR,ko;q=0.8,en-US;q=0.5",
        "Origin":"https://www.musinsa.com",
        "Referer":"https://www.musinsa.com/",
    }, cookie_header or "", auth_payload)
    
    _log_api_call("fetch_payment_events", config.PAYMENT_EVENT_URL, headers=headers)
    r = _http().get(config.PAYMENT_EVENT_URL, headers=headers, timeout=15)
    _log_api_call("fetch_payment_events", config.PAYMENT_EVENT_URL, headers=headers, response=r)

    r.raise_for_status()
    _PAYMENT_EVENT_CACHE = r.json()
    return _PAYMENT_EVENT_CACHE

def build_promotion_groups(ev_json: dict) -> List[dict]:
    groups = []
    for p in (ev_json.get("data") or {}).get("paymentPromotionList", []):
        name = p.get("contentName")
        items = []
        for txt in (p.get("contentTextList") or []):
            rule = _parse_rule_line(txt)
            if rule:
                items.append({
                    "min_amount": int(rule["threshold"]),
                    "discount": int(rule["discount"]),
                    "label": txt,
                    "checked": False,
                })
        if items:
            groups.append({"group": name, "items": items})
    return groups

def compute_promotion_discount(base_amount: int, selected_items: List[dict]) -> int:
    if base_amount <= 0 or not selected_items: return 0
    total = 0
    for it in selected_items:
        if base_amount >= int(it.get("min_amount",0) or 0):
            total += int(it.get("discount",0) or 0)
    return max(0, min(total, base_amount))

def _deep_find_style_no(obj: Any) -> Optional[str]:
    if isinstance(obj, dict):
        if "styleNo" in obj and isinstance(obj["styleNo"], str) and obj["styleNo"]:
            return obj["styleNo"]
        for v in obj.values():
            hit = _deep_find_style_no(v)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _deep_find_style_no(v)
            if hit:
                return hit
    return None

def extract_style_no_from_response_text(text: str) -> Optional[str]:
    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>({.*?})</script>',
        text,
        flags=re.S
    )
    if m:
        try:
            data = json.loads(m.group(1))
            hit = _deep_find_style_no(data)
            if hit:
                return hit.strip()
        except Exception:
            pass

    m2 = re.search(r'"styleNo"\s*:\s*"([A-Za-z0-9\-/]+)"', text)
    if m2:
        return m2.group(1).strip()

    m3 = re.search(r'/\s*([A-Za-z0-9\-/]+)\s*-\s', text)
    if m3:
        return m3.group(1).strip()
    m4 = re.search(r'(?:제품번호\s*[:：]\s*)([A-Za-z0-9\-/]+)', text)
    if m4:
        return m4.group(1).strip()
    return None

def _fetch_serial_with_script(goods_no: str) -> Optional[str]:
    """
    Executes the musinsa_serial.py script to get the serial number.
    """
    command = [sys.executable, "-m", "shop_comparator.shops.musinsa_serial", str(goods_no)]
    try:
        print(f"[INFO] Running script to get serial: {' '.join(command)}")
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=True  # Raise exception for non-zero exit codes
        )
        serial = proc.stdout.strip()
        if serial:
            print(f"[INFO] Script successful for {goods_no}, serial: {serial}")
            return serial
        else:
            print(f"[WARN] Script for {goods_no} ran but returned empty output.")
            print(f"[WARN] Stderr: {proc.stderr.strip()}")
            return None
    except FileNotFoundError:
        print(f"[ERROR] musinsa_serial.py not found. Cannot get serial number.")
        return None
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Script execution failed for {goods_no} with exit code {e.returncode}.")
        print(f"[ERROR] Stderr: {e.stderr.strip()}")
        return None
    except Exception as e:
        print(f"[ERROR] An unexpected error occurred while running script for {goods_no}: {e}")
        return None

def _fetch_serial_number_robust(sess: requests.Session, goods_no: str) -> Optional[str]:
    """
    Fetches the product page HTML using the existing requests session and extracts the serial number.
    Target URL: https://www.musinsa.com/products/{goods_no}
    """
    url = f"https://www.musinsa.com/products/{goods_no}"
    print(f"[SERIAL_FETCH] Fetching HTML for goods_no: {goods_no}")
    
    try:
        # 헤더를 명시적으로 설정하여 브라우저 요청처럼 보이게 함
        headers = sess.headers.copy()
        headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Referer": "https://www.musinsa.com/",
            "Upgrade-Insecure-Requests": "1"
        })
        
        r = sess.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        html = r.text
        
        # 1. 메타 태그 description에서 검색 (사용자가 제공한 패턴)
        # <meta name="description" content="... 제품번호 : 1174470-CHE ..." />
        m_desc = re.search(r'name="description"\s+content="[^"]*제품번호\s*[:：]\s*([A-Za-z0-9\-_/]+)', html)
        if m_desc:
            serial = m_desc.group(1).strip()
            print(f"[SERIAL_FETCH] SUCCESS (Meta Description): {serial}")
            return serial

        # 2. 본문 텍스트에서 검색 (기존 패턴 유지 및 확장)
        return extract_style_no_from_response_text(html)

    except Exception as e:
        print(f"[SERIAL_FETCH] Error fetching product page: {e}")
        return None

# ---------- 수집 파이프 (한 상품) ----------
def price_option(cookie_header: str, goods_no: str, goods_option_no: str, auth_payload: Optional[dict] = None, sleep_each=0.5) -> dict:
    """Member price of exactly one option, read from that option's own order form.

    Puts the option in the "buy now" cart (isOrder=true) and reads the order
    form; no order is placed.  Raises instead of falling back to public or
    other-option prices.
    """
    jr = add_to_cart_raw(cookie_header, goods_no, goods_option_no, auth_payload=auth_payload)
    if not (jr.get("data") and jr["data"].get("cartIds")):
        raise CrawlError("무신사", "pricing", f"카트 담기 실패: {jr.get('meta', {}).get('message') or '알 수 없는 오류'}")
    time.sleep(sleep_each)

    of = fetch_order_form(cookie_header, auth_payload=auth_payload)
    time.sleep(sleep_each)
    item = None
    for it in of.get("cartItems") or []:
        if str(it.get("goodsNo")) == str(goods_no) and str(it.get("goodsOptionNo")) == str(goods_option_no):
            item = it; break
    if item is None:
        raise CrawlError("무신사", "pricing", "주문 폼에서 상품을 찾을 수 없음")

    sale_price = int(item.get("salePrice",0) or 0)
    if sale_price <= 0:
        raise CrawlError("무신사", "schema", "해당 옵션의 판매가를 확인하지 못했습니다.")
    flags = {
        "memberDiscountRate": float(item.get("memberDiscountRate",0.0) or 0.0),
        "memberSavePointRate": float(item.get("memberSavePointRate",0.0) or 0.0),
        "isUsePointDiscount": bool(item.get("isPrePointDiscount", False)),
        "isMemberDiscount": bool(item.get("isMemberDiscount",False)),
        "goodsSavePoint": int(item.get("goodsSavePoint",0) or 0),
        "isCouponDiscount": bool(item.get("isCouponDiscount", False)),
    }
    member_info = of.get("member") or {}

    coupons = fetch_goods_coupons(cookie_header, goods_no, sale_price, item.get("brandId"), item.get("companyId"), flags["isCouponDiscount"], auth_payload=auth_payload)
    time.sleep(sleep_each)

    best = pick_best_coupon(coupons)
    if best:
        coupon_discount = int(best.get("salePrice",0) or 0)
        coupon_name     = best.get("couponName")
        coupon_vkind    = f"{best.get('couponValue')}{best.get('couponKind')}"
    else:
        coupon_discount = 0; coupon_name=""; coupon_vkind=""

    calc = compute_final_v2(sale_price, coupon_discount, flags, member_info=member_info)
    _debug(
        f"[PRICE_CALC] goods_no={goods_no} option={goods_option_no} sale={sale_price} "
        f"coupon={coupon_discount} member_discount={calc['member_discount_amount']} "
        f"point_discount={calc['point_discount_amount']} final={calc['final_price']}"
    )
    return {
        "salePrice": sale_price,
        "couponDiscount": coupon_discount,
        "finalPrice": calc["final_price"],
        "priceStatus": "verified",
        "memberDiscountAmount": calc["member_discount_amount"],
        "pointDiscountAmount": calc["point_discount_amount"],
        "memberDiscountRate": flags["memberDiscountRate"],
        "memberSavePointRate": flags["memberSavePointRate"],
        "isMemberDiscount": flags["isMemberDiscount"],
        "isUsePointDiscount": flags["isUsePointDiscount"],
        "isCouponDiscount": flags["isCouponDiscount"],
        "goodsSavePoint": flags["goodsSavePoint"],
        "couponName": coupon_name,
        "couponValueKind": coupon_vkind,
        "couponCount": len(coupons),
        "baseCategoryFullName": item.get("baseCategoryFullName") or "",
    }


def process_one(sess: requests.Session, cookie_header: str, goods_no: str, link_url: str, detail_meta: dict, sleep_each=0.5, auth_payload: Optional[dict] = None, selected_option=None, options_payload=None, known_serial=None) -> dict:
    # 기존 curl 방식 대신 requests 세션을 이용한 robust 함수 호출
    serial_number = known_serial or _fetch_serial_number_robust(sess, goods_no)
    
    _debug(f"[MUSINSA_CRAWL] 상품번호: {goods_no}, 품번: {serial_number if serial_number else '추출 실패'}")

    try:
        opts = options_payload if options_payload is not None else fetch_options_any(sess, goods_no)
        _debug(f"\n[GoodsNo {goods_no}] 사이즈별 재고 상태")
        _debug("-" * 60)
        _debug(f"{'SIZE':>10} | {'variantId':>12} | {'STATUS':>9} | ETA")
        _debug("-" * 60)
        
        items = (opts.get("data") or {}).get("optionItems") or []
        
        def safe_int_local(s):
            try:
                return int(s)
            except (ValueError, TypeError, AttributeError):
                return None

        def sort_key(row):
            size_val = "N/A"
            try:
                size_val = row.get("managedCode") or (row.get("optionValues") or [{}])[0].get("name")
            except (IndexError, AttributeError):
                pass
            n = safe_int_local(size_val)
            return (0, n) if n is not None else (1, size_val)

        try:
            sorted_items = sorted(items, key=sort_key)
        except Exception:
            sorted_items = items

        if sorted_items:
            for item in sorted_items:
                variant_id = item.get("no", "N/A")
                size = "N/A"
                try:
                    size = item.get("managedCode") or (item.get("optionValues") or [{}])[0].get("name")
                except (IndexError, AttributeError):
                    pass
                
                is_sold_out = item.get("isSoldOut")
                status = "SOLD_OUT" if is_sold_out else "IN_STOCK"
                eta = item.get('eta') or '-'
                
                _debug(f"{str(size):>10} | {str(variant_id):>12} | {status:>9} | {eta}")
        else:
            _debug("  사이즈 정보를 찾을 수 없습니다.")
        _debug("-" * 60)
        time.sleep(sleep_each)
        combo = selected_option or choose_sellable_option(opts)
        sizes = extract_available_sizes(opts)
    except Exception:
        raise
    soldout = (combo is None)

    base = {
        "goodsNo": goods_no,
        "linkUrl": link_url,
        "goodsName": detail_meta.get("goodsName"),
        "brandName": detail_meta.get("brandName"),
        "serial_number": serial_number,
        "imageUrl": detail_meta.get("imageUrl"),
        "normalPrice": detail_meta.get("normalPrice"),
        "saleRate": detail_meta.get("saleRate"),
        "availableSizes": ",".join(sizes) if sizes else "",
        "soldout": soldout,
        "baseCategoryFullName": "",
    }

    if soldout:
        base.update({
            "salePrice": None, "couponDiscount": None, "finalPrice": None,
            "memberDiscountAmount": None, "pointDiscountAmount": None,
            "memberDiscountRate":None, "memberSavePointRate":None,
            "isMemberDiscount":None, "isUsePointDiscount":None,
            "isCouponDiscount":None,
            "goodsSavePoint":None, "couponName":None, "couponValueKind":None
        })
        return base

    managed_code, goods_option_no, raw_item = combo

    try:
        priced = price_option(cookie_header, goods_no, goods_option_no, auth_payload=auth_payload, sleep_each=sleep_each)
        base["baseCategoryFullName"] = priced.pop("baseCategoryFullName", "")
        base.update(priced)
        base.update({"managedCode": managed_code, "goodsOptionNo": goods_option_no})
        return base

    except Exception as e:
        _debug(f"\nERROR in process_one for goods_no {goods_no}: {e}\n") # Add error logging
        fallback_sale_price = (
            detail_meta.get("price")
            or detail_meta.get("couponPrice")
            or detail_meta.get("normalPrice")
        )
        fallback_final_price = (
            detail_meta.get("couponPrice")
            or detail_meta.get("price")
            or detail_meta.get("normalPrice")
        )
        base.update({
            "salePrice": fallback_sale_price,
            "couponDiscount": None,
            "finalPrice": None,
            "publicPrice": fallback_final_price,
            "priceStatus": "error",
            "memberDiscountAmount": None,
            "pointDiscountAmount": None,
            "memberDiscountRate":None, "memberSavePointRate":None,
            "isMemberDiscount":None, "isUsePointDiscount":None,
            "isCouponDiscount":None,
            "goodsSavePoint":None, "couponName":None, "couponValueKind":None,
            "error": str(e)
        })
        return base

def fetch_goods_from_style_page(sess: requests.Session, style_url: str, log_callback=None, page_limit: int = 5) -> List[str]:
    def log(msg):
        if log_callback: log_callback(msg)

    goods_ids = []
    base_url = "https://www.musinsa.com"
    
    for page in range(1, page_limit + 1):
        full_url = f"{base_url}{style_url}&p={page}"
        log(f"[INFO] Fetching style page: {full_url}")
        try:
            _log_api_call("fetch_goods_from_style_page", full_url, headers=sess.headers)
            res = sess.get(full_url, timeout=15)
            _log_api_call("fetch_goods_from_style_page", full_url, headers=sess.headers, response=res)
            res.raise_for_status()
            data = res.json()
            
            items = data.get("data", {}).get("goods", [])
            if not items:
                log(f"[INFO] No more items found on page {page}. Stopping.")
                break
            
            for item in items:
                if item and item.get("goods_no"):
                    goods_ids.append(str(item.get("goods_no")))

        except requests.exceptions.RequestException as e:
            log(f"[ERROR] Error fetching style page {full_url}: {e}")
            break
        except (json.JSONDecodeError, AttributeError) as e:
            log(f"[ERROR] Error parsing JSON from {full_url}: {e}")
            break
            
    return list(dict.fromkeys(goods_ids))


# ---------- 카테고리 / 상세 ----------
def fetch_category_items(sess: requests.Session, category_codes: List[str], top_n: int, log_callback=None, strict=False) -> List[Tuple[int, str, str]]:
    def log(msg):
        if log_callback: log_callback(msg)

    if not category_codes:
        return []
    cat_code = category_codes[0]

    pids_for_this_category = {}
    api_url = "https://api.musinsa.com/api2/dp/v1/plp/goods"
    PAGE_SIZE = 60
    
    try:
        page = 1
        while len(pids_for_this_category) < top_n:
            params = {
                "gf": "A",
                "sortCode": "POPULAR",
                "category": cat_code,
                "size": PAGE_SIZE,
                "caller": "CATEGORY",
                "page": page
            }
            
            _log_api_call("fetch_category_items", api_url, params=params, headers=sess.headers)
            r = sess.get(api_url, params=params, timeout=15)
            _log_api_call("fetch_category_items", api_url, params=params, headers=sess.headers, response=r)

            data = response_json(r)

            goods_list = data.get("data", {}).get("list", [])
            if not goods_list:
                log(f"[INFO] 카테고리 {cat_code}의 페이지 {page}에서 더 이상 상품을 찾지 못했습니다.")
                break

            previous_count = len(pids_for_this_category)
            for item in goods_list:
                pid = str(item.get("goodsNo"))
                if pid and pid != "None":
                    pids_for_this_category[pid] = None
                    if len(pids_for_this_category) >= top_n:
                        break
            
            if len(pids_for_this_category) == previous_count:
                break
            page += 1
            time.sleep(0.3)

    except Exception as e:
        log(f"[ERROR] 카테고리 {cat_code} 상품 ID 수집 중 오류 발생: {e}")
        if strict:
            raise

    final_pids = list(pids_for_this_category)[:top_n]
    return [(i+1, pid, f"https://www.musinsa.com/app/goods/{pid}") for i, pid in enumerate(final_pids)]

# ---------- 전체 실행 함수 (랭킹 기반) ----------
def _pick_items_from_sections_api(obj: dict) -> List[Tuple[int, str, str]]:
    """Parses the new sections API response."""
    items = []
    try:
        for module in obj.get("data", {}).get("modules", []):
            for item in module.get("items", []):
                if item.get("type") == "PRODUCT_COLUMN":
                    product_id = str(item.get("id"))
                    rank = item.get("image", {}).get("rank")
                    if product_id and rank is not None:
                        link = f"https://www.musinsa.com/products/{product_id}"
                        items.append((rank, product_id, link))
    except Exception as e:
        print(f"[ERROR] _pick_items_from_sections_api parsing error: {e}")
    return items

def run_ranking_crawl(cookie_header: str, ranking_api_url: str, top_n: int, log_callback=None, progress_callback=None, auth_payload: Optional[dict] = None) -> List[dict]:
    
    def log(msg):
        if log_callback: log_callback(msg)

    all_items = []
    log("[INFO] 랭킹 기반 수집 시작 (신규 API 방식 v2)...")
    s = build_session(cookie_header, auth_payload=auth_payload)
    
    try:
        # --- Construct the new API URL from the old one ---
        query_params_str = ranking_api_url.split('?')[1] if '?' in ranking_api_url else ""
        section_id_match = re.search(r'sectionId=(\d+)', query_params_str)
        if not section_id_match:
            raise ValueError("ranking_api_url에서 sectionId를 찾을 수 없습니다.")
        section_id = section_id_match.group(1)

        new_api_base_url = f"https://api.musinsa.com/api2/hm/web/v5/pans/ranking/sections/{section_id}"

        # --- Paginate and Fetch ---
        all_rank_items = []
        seen_pids = set()
        page = 1
        log(f"[INFO] 랭킹 API 호출 시작 (최대 {top_n}개 목표)")
        
        while len(all_rank_items) < top_n:
            offset = len(all_rank_items)
            start_rank = offset + 1

            params = f"{query_params_str}&page={page}&startRank={start_rank}&offset={offset}"
            paginated_url = f"{new_api_base_url}?{params}"
            
            log(f"[INFO] 랭킹 페이지 {page} 호출 (offset: {offset})...")

            _log_api_call("run_ranking_crawl", paginated_url, headers=s.headers)
            r = s.get(paginated_url, timeout=15)
            _log_api_call("run_ranking_crawl", paginated_url, headers=s.headers, response=r)
            r.raise_for_status()
            j = r.json()

            current_page_items = _pick_items_from_sections_api(j)
            if not current_page_items:
                log(f"[INFO] 랭킹 페이지 {page}에서 더 이상 상품을 찾지 못했습니다. 수집을 중단합니다.")
                break
            
            newly_added_count = 0
            for item in current_page_items:
                pid = item[1]
                if pid not in seen_pids:
                    seen_pids.add(pid)
                    all_rank_items.append(item)
                    newly_added_count += 1

            if newly_added_count == 0 and page > 1:
                log("[WARN] 새로운 상품이 발견되지 않았습니다. API가 같은 페이지만 반환하는 것 같습니다.")
                break

            log(f"[INFO] 페이지 {page}에서 {newly_added_count}개 신규 상품 발견. 현재까지 총 {len(all_rank_items)}개.")
            
            if len(all_rank_items) >= top_n:
                break
            
            page += 1
            time.sleep(0.3)

        rank_items = sorted(all_rank_items, key=lambda x: x[0])[:top_n]
        
        ids = [pid for _,pid,_ in rank_items]
        log(f"[INFO] 최종 랭킹 {len(rank_items)}개 대상 확인. 상세 정보 수집 중…")
        detail_map = fetch_details(s, ids)
        log("[INFO] 상세 정보 수집 완료.")

    except Exception as e:
        log(f"[ERROR] 랭킹/상세 수집 실패: {e}")
        return []

    # --- Process items (same as before) ---
    for i, (rnk, goods_no, link) in enumerate(rank_items):
        dmeta = detail_map.get(str(goods_no), {})
        log(f"[INFO] ({i+1}/{len(rank_items)}) 상품 처리 시작: {goods_no}")
        try:
            row = process_one(s, cookie_header, goods_no, link, dmeta, sleep_each=0.5, auth_payload=auth_payload)
        except Exception as e:
            row = {"goodsNo": goods_no, "linkUrl": link, "goodsName": dmeta.get("goodsName"), "brandName": dmeta.get("brandName"), "error": str(e)}
            log(f"[WARN] 처리 실패 goodsNo={goods_no}: {e}")

        row["rank"] = rnk
        all_items.append(row)
        if progress_callback:
            progress_callback(row)

    log("[OK] 수집 완료.")
    return all_items

# ---------- 전체 실행 함수 (카테고리 기반) ----------
def run_full_crawl(cookie_header: str, category_codes: List[str], top_n: int, log_callback=None, progress_callback=None, auth_payload: Optional[dict] = None) -> List[dict]:
    
    def log(msg):
        if log_callback: log_callback(msg)

    all_items_map = {} 
    log("[INFO] 수집 시작…")
    s = build_session(cookie_header, auth_payload=auth_payload)
    
    total_item_count = 0
    for cat_code in category_codes:
        log(f"--- 카테고리 [{cat_code}] 크롤링 시작 ---")
        try:
            rank_items = fetch_category_items(s, [cat_code], top_n, log_callback=log)
            if not rank_items:
                log(f"[WARN] 카테고리 [{cat_code}]에서 수집된 상품이 없습니다. 다음 카테고리로 넘어갑니다.")
                continue
                
            ids = [pid for _,pid,_ in rank_items]
            log(f"[INFO] 카테고리 [{cat_code}]에서 {len(rank_items)}개 대상 확인. 상세 정보 수집 중…")
            
            detail_map = fetch_details(s, ids)
            log(f"[INFO] 카테고리 [{cat_code}] 상세 정보 수집 완료.")

        except Exception as e:
            log(f"[ERROR] 카테고리 [{cat_code}] 상품 수집/상세 정보 수집 실패: {e}")
            continue 

        for i, (rnk, goods_no, link) in enumerate(rank_items):
            dmeta = detail_map.get(str(goods_no), {})
            total_item_count += 1
            log(f"[INFO] (전체 {total_item_count}번째, 카테고리 내 {i+1}/{len(rank_items)}) 상품 처리 시작: {goods_no}")
            try:
                row = process_one(s, cookie_header, goods_no, link, dmeta, sleep_each=0.5, auth_payload=auth_payload)
            except Exception as e:
                row = {
                    "goodsNo": goods_no, "linkUrl": link,
                    "goodsName": dmeta.get("goodsName"), "brandName": dmeta.get("brandName"),
                    "error": str(e)
                }
                log(f"[WARN] 처리 실패 goodsNo={goods_no}: {e}")

            row["rank"] = rnk 
            
            if "goodsNo" in row and row["goodsNo"]:
                all_items_map[row["goodsNo"]] = row

            if progress_callback:
                progress_callback(row)
        
        log(f"--- 카테고리 [{cat_code}] 크롤링 완료 ---")

    final_items = list(all_items_map.values())
    log(f"[OK] 전체 수집 완료. 총 {len(final_items)}개 상품 (중복 제거됨).")
    return final_items

# ---------- 신규: 브랜드 기반 크롤링 ----------
def get_products_by_brand(brand_name: str, count: int, log_callback=None) -> List[dict]:
    """
    Fetches a list of products for a specific brand from Musinsa's API,
    handling pagination for large requests.
    """
    def log(msg):
        if log_callback: log_callback(msg)

    all_products = []
    page = 1
    PAGE_SIZE = 100  # API seems to have a limit around 100

    log(f"[INFO] 브랜드 '{brand_name}'의 상품을 최대 {count}개까지 수집합니다.")

    while len(all_products) < count:
        api_url = "https://api.musinsa.com/api2/dp/v1/plp/goods"
        params = {
            "brand": brand_name,
            "sortCode": "POPULAR",
            "page": page,
            "size": PAGE_SIZE,
            "caller": "FLAGSHIP",
            "gf": "A"
        }
        headers = {
            "User-Agent": config.UA,
            "Accept": "application/json",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
            "Origin": "https://www.musinsa.com",
        }

        log(f"[INFO] 브랜드 '{brand_name}'의 상품 페이지 {page} (사이즈: {PAGE_SIZE})를 API로 요청합니다.")
        _log_api_call("get_products_by_brand", api_url, params=params, headers=headers)

        try:
            r = _http().get(api_url, params=params, headers=headers, timeout=20)
            _log_api_call("get_products_by_brand", api_url, params=params, headers=headers, response=r)
            r.raise_for_status()
            data = r.json()
            
            product_list = data.get("data", {}).get("list", [])
            if not product_list:
                log(f"[INFO] 브랜드 '{brand_name}'의 페이지 {page}에서 더 이상 상품을 찾지 못했습니다.")
                break

            all_products.extend(product_list)
            log(f"[INFO] 브랜드 '{brand_name}'에서 {len(product_list)}개의 상품 정보를 추가로 수신했습니다. (총 {len(all_products)}개)")
            
            page += 1
            time.sleep(0.3)  # Be polite to the server

        except requests.exceptions.RequestException as e:
            log(f"[ERROR] 브랜드 '{brand_name}' 상품 API 요청 실패: {e}")
            break  # Stop on error
        except json.JSONDecodeError:
            log(f"[ERROR] 브랜드 '{brand_name}' API 응답이 유효한 JSON이 아닙니다.")
            break
        except Exception as e:
            log(f"[ERROR] 브랜드 '{brand_name}' 상품 수집 중 알 수 없는 오류 발생: {e}")
            break
    
    return all_products[:count]

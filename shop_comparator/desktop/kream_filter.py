


# -*- coding: utf-8 -*-

from __future__ import annotations

import json, time, uuid, os, sys, random
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
import requests

_module_ua = None
_module_kream_api_version = None
_module_kream_web_request_sec = None
_module_kream_web_build_ver = None
_module_kream_device_id = None
_module_access_token = None

def init_rotate_module(ua: str, api_version: str, web_request_sec: str, web_build_ver: str, device_id: str, access_token: str):
    global _module_ua, _module_kream_api_version, _module_kream_web_request_sec, _module_kream_web_build_ver, _module_kream_device_id, _module_access_token
    _module_ua = ua
    _module_kream_api_version = api_version
    _module_kream_web_request_sec = web_request_sec
    _module_kream_web_build_ver = web_build_ver
    _module_kream_device_id = device_id
    _module_access_token = access_token

def kst_now_str():
    kst = timezone(timedelta(hours=9))
    return datetime.now(kst).strftime("%Y%m%d%H%M%S+0900")

def sleep_jitter(a=0.15, b=0.35):
    time.sleep(random.uniform(a, b))

BASE_URL = "https://api.kream.co.kr/api/p/products/{product_id}/sales"

def build_headers(product_id: int, sentry_trace: str | None = None, baggage: str | None = None) -> dict:
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "authorization": f"Bearer {_module_access_token}",
        "origin": "https://kream.co.kr",
        "referer": f"https://kream.co.kr/products/{product_id}?fetchRelated=true",
        "sec-ch-ua": "\"Not;A=Brand\";v=\"99\", \"Google Chrome\";v=\"139\", \"Chromium\";v=\"139\"",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "\"Windows\"",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": _module_ua,
        "x-kream-api-version": _module_kream_api_version,
        "x-kream-client-datetime": kst_now_str(),
        "x-kream-device-id": _module_kream_device_id,
        "x-kream-web-build-version": _module_kream_web_build_ver,
        "x-kream-web-request-secret": _module_kream_web_request_sec,
    }

def fetch_sales(product_id: int):
    url = BASE_URL.format(product_id=product_id)
    combined_items = []
    first_response_data = None

    for cursor_page in ["1", "2"]:
        params = {
            "cursor": cursor_page,
            "per_page": "50",
            "sort": "date_created[desc]",
            "request_key": str(uuid.uuid4()),
        }

        response = None
        for attempt in range(4):
            headers = build_headers(product_id)
            response = requests.get(url, headers=headers, params=params, timeout=15)

            if response.status_code == 404 and cursor_page == "2":
                break

            if response.status_code in (503, 502, 500, 429):
                sleep_jitter(0.3*(2**attempt), 0.6*(2**attempt))
                params["request_key"] = str(uuid.uuid4())
                continue

            if response.status_code == 401:
                print(f"[401] product_id={product_id} — UA/Device/토큰 조합 불일치 또는 만료.")
                return first_response_data if first_response_data else None

            response.raise_for_status()
            data = response.json()

            if cursor_page == "1" and not first_response_data:
                first_response_data = data
            
            items = data.get("items", [])
            if items:
                combined_items.extend(items)
            
            break
        
        if response and response.status_code == 404 and cursor_page == "2":
            break

        if not first_response_data:
            print(f"[FAIL] product_id={product_id} — cursor=1에서 데이터를 가져오지 못했습니다.")
            return None

    if first_response_data:
        first_response_data["items"] = combined_items

    return first_response_data

def check_product(product_id: int, min_total_deals: int, min_monthly_deals: int) -> bool:
    data = fetch_sales(product_id)
    if not data:
        return True

    total_deals = data.get("total", 0)
    if total_deals < min_total_deals:
        return False

    now = datetime.now(timezone.utc)
    one_month_ago = now - timedelta(days=30)
    monthly_deals = 0
    for item in data.get("items", []):
        created_at_str = item.get("date_created")
        if created_at_str:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            if created_at >= one_month_ago:
                monthly_deals += 1

    return monthly_deals >= min_monthly_deals

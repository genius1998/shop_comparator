# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

from shop_comparator.shops.kream_crawler import KreamAuthException


_module_ua = None
_module_kream_api_version = None
_module_kream_web_request_sec = None
_module_kream_web_build_ver = None
_module_kream_device_id = None
_module_access_token = None

BASE_URL = "https://api.kream.co.kr/api/p/products/{product_id}/sales"


def init_rotate_module(
    ua: str,
    api_version: str,
    web_request_sec: str,
    web_build_ver: str,
    device_id: str,
    access_token: str,
):
    global _module_ua
    global _module_kream_api_version
    global _module_kream_web_request_sec
    global _module_kream_web_build_ver
    global _module_kream_device_id
    global _module_access_token

    _module_ua = ua
    _module_kream_api_version = api_version
    _module_kream_web_request_sec = web_request_sec
    _module_kream_web_build_ver = web_build_ver
    _module_kream_device_id = device_id
    _module_access_token = access_token

    masked_token = f"{access_token[:6]}...{access_token[-4:]}" if access_token else "missing"
    print("init_rotate_module called")
    print(f"_module_ua:{_module_ua}")
    print(f"_module_access_token:{masked_token}")
    print(f"_module_kream_device_id:{_module_kream_device_id}")


def kst_now_str():
    kst = timezone(timedelta(hours=9))
    return datetime.now(kst).strftime("%Y%m%d%H%M%S+0900")


def sleep_jitter(a=0.15, b=0.35):
    time.sleep(random.uniform(a, b))


def build_headers(product_id: int, sentry_trace: str | None = None, baggage: str | None = None) -> dict:
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "authorization": f"Bearer {_module_access_token}",
        "origin": "https://kream.co.kr",
        "referer": f"https://kream.co.kr/products/{product_id}?fetchRelated=true",
        "sec-ch-ua": '"Not;A=Brand";v="99", "Google Chrome";v="139", "Chromium";v="139"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
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
    if sentry_trace:
        headers["sentry-trace"] = sentry_trace
    if baggage:
        headers["baggage"] = baggage
    return headers


def fetch_sales(product_id: int):
    sleep_jitter(0.5, 1.0)
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
                sleep_jitter(0.3 * (2**attempt), 0.6 * (2**attempt))
                params["request_key"] = str(uuid.uuid4())
                continue

            if response.status_code == 401:
                raise KreamAuthException("KREAM token authentication failed (401)")

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
            print(f"[FAIL] product_id={product_id} cursor=1 returned no data")
            return None

    if first_response_data:
        first_response_data["items"] = combined_items

    return first_response_data


def save_json(pid: int, data: dict):
    os.makedirs("out", exist_ok=True)
    path = os.path.join("out", f"sales_{pid}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


PRODUCT_IDS = [
    416242,
    300622,
    593710,
    15251,
    21935,
    329970,
    40814,
    338430,
    12831,
    247990,
    556043,
    425658,
    531485,
    153967,
    34146,
    351030,
    30054,
]


if __name__ == "__main__":
    ids = [int(x) for x in sys.argv[1:]] if len(sys.argv) > 1 else PRODUCT_IDS

    for pid in ids:
        time.sleep(1)
        data = fetch_sales(pid)
        if not data:
            continue
        path = save_json(pid, data)
        items = data.get("items", [])
        print(f"OK {pid}: total={data.get('total')} returned={len(items)} -> {path}")
        for item in items[:5]:
            print(
                f"  option={item.get('option')} "
                f"price={item.get('price')} "
                f"time={item.get('date_created')}"
            )
        print("-" * 60)

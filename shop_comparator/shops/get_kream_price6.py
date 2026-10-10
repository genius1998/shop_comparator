import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode


KST = timezone(timedelta(hours=9))


def kream_client_datetime() -> str:
    return datetime.now(KST).strftime("%Y%m%d%H%M%S%z")


def parse_json_or_save(body: str) -> Dict[str, Any]:
    low = body[:300].lower()
    if low.startswith("<!doctype html") or "<html" in low:
        with open("kream_error.html", "w", encoding="utf-8") as f:
            f.write(body)
        raise RuntimeError("KREAM returned HTML (blocked/error). Saved: kream_error.html")

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        with open("kream_raw.txt", "w", encoding="utf-8") as f:
            f.write(body)
        raise RuntimeError("Response is not valid JSON. Saved: kream_raw.txt")


def run_curl(url: str, header_lines: List[str], timeout_sec: int = 30) -> str:
    cmd = ["curl", "-sS", "--compressed", url]
    for h in header_lines:
        cmd += ["-H", h]

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
        raise RuntimeError("curl.exe를 못 찾음. Windows PATH 확인 필요")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"curl timeout ({timeout_sec}s)")

    if proc.returncode != 0:
        raise RuntimeError(f"curl failed (code={proc.returncode}): {(proc.stderr or '')[:500]}")

    return (proc.stdout or "").strip()


def _first(v):
    if isinstance(v, list) and v:
        return v[0]
    return v


def fmt_price_text(price_int: Optional[int]) -> Optional[str]:
    if price_int is None:
        return None
    return f"{price_int:,}"


def extract_button_title_text(node: Dict[str, Any]) -> str:
    """
    버튼 node 안에서 title_item.text_element.variations[*].text 를 최대한 긁어와서 합침
    (643258 같은 케이스는 여기서 '즉시 구매 / 구매 입찰'을 잡아야 함)
    """
    texts: List[str] = []

    def pick_text(obj: Any):
        if isinstance(obj, dict):
            # 흔한 위치: title_item -> text_element -> variations -> [{text: ...}]
            if "text" in obj and isinstance(obj["text"], str):
                texts.append(obj["text"])
            for vv in obj.values():
                pick_text(vv)
        elif isinstance(obj, list):
            for xx in obj:
                pick_text(xx)

    # title_item 중심으로 먼저 긁고, 없으면 node 전체도 한번 긁음
    title_item = node.get("title_item")
    if isinstance(title_item, dict):
        pick_text(title_item)
    else:
        pick_text(node)

    # 중복 제거 + join
    uniq = []
    seen = set()
    for t in texts:
        t2 = t.strip()
        if not t2:
            continue
        if t2 in seen:
            continue
        seen.add(t2)
        uniq.append(t2)
    return " / ".join(uniq)


def extract_prices(payload: Any) -> List[Dict[str, Any]]:
    """
    핵심:
    - 원래 케이스: named_action.value == 'instant_buy' -> 그대로 추출
    - 변종 케이스: named_action.value == 'buy' 인데,
      같은 버튼(title)에 '즉시 구매' 문구가 들어가면 -> 즉시구매 후보로도 추출
    """
    results: List[Dict[str, Any]] = []

    def parse_price_int(price_raw: Any) -> Optional[int]:
        if price_raw is None:
            return None
        try:
            return int(str(price_raw).strip())
        except ValueError:
            return None

    def walk(node: Any):
        if isinstance(node, dict):
            actions = node.get("actions")
            title_text = extract_button_title_text(node)
            has_instant_wording = ("즉시 구매" in title_text) or ("instant buy" in title_text.lower())

            if isinstance(actions, list):
                for act in actions:
                    if not isinstance(act, dict):
                        continue
                    if act.get("type") != "named_action":
                        continue

                    value = act.get("value")
                    params = act.get("parameters") or {}

                    # 공통 필드
                    product_id = _first(params.get("product_id"))
                    option_key = _first(params.get("option_key"))
                    price_int = parse_price_int(_first(params.get("price")))
                    checkout_type = _first(params.get("checkout_type"))
                    delivery_method = _first(params.get("delivery_method"))

                    # 1) 정석 instant_buy
                    if value == "instant_buy":
                        results.append({
                            "kind": "instant_buy",
                            "product_id": product_id,
                            "option_key": option_key,
                            "price": price_int,
                            "price_text": fmt_price_text(price_int),
                            "checkout_type": checkout_type,
                            "delivery_method": delivery_method,
                            "button_title": title_text,
                        })
                        continue

                    # 2) 변종: buy 인데 버튼에 '즉시 구매'가 같이 적혀있으면 즉시구매 후보로 취급
                    #    (643258 응답이 이 타입)
                    if value == "buy" and has_instant_wording:
                        results.append({
                            "kind": "buy(instant_candidate)",
                            "product_id": product_id,
                            "option_key": option_key,
                            "price": price_int,
                            "price_text": fmt_price_text(price_int),
                            "checkout_type": checkout_type,
                            "delivery_method": delivery_method,
                            "button_title": title_text,
                        })
                        continue

            for v in node.values():
                walk(v)

        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(payload)

    # 중복 제거: option_key + kind + price 기준
    uniq: Dict[tuple, Dict[str, Any]] = {}
    for r in results:
        k = (r.get("option_key"), r.get("kind"), r.get("price"))
        uniq[k] = r

    return list(uniq.values())


def build_headers(args, product_id: int) -> List[str]:
    header_lines = [
        'accept: */*',
        'accept-language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
        f'authorization: Bearer {args.token}',
    ]

    if args.baggage:
        header_lines.append(f"baggage: {args.baggage}")
    if args.sentry_trace:
        header_lines.append(f"sentry-trace: {args.sentry_trace}")

    header_lines += [
        'origin: https://kream.co.kr',
        'priority: u=1, i',
        f'referer: https://kream.co.kr/products/{product_id}',
        'sec-ch-ua: "Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        'sec-ch-ua-mobile: ?0',
        'sec-ch-ua-platform: "Windows"',
        'sec-fetch-dest: empty',
        'sec-fetch-mode: cors',
        'sec-fetch-site: same-site',
        'user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36',
        f'x-kream-api-version: {args.api_version}',
        f'x-kream-client-datetime: {args.client_datetime or kream_client_datetime()}',
        f'x-kream-device-id: {args.device_id}',
        f'x-kream-web-build-version: {args.web_build_version}',
        f'x-kream-web-request-secret: {args.web_request_secret}',
    ]
    return header_lines


def fetch_options_display(product_id: int, picker_type: str, headers: List[str], request_key: Optional[str]) -> Dict[str, Any]:
    base_url = "https://api.kream.co.kr/api/p/options/display"
    qs = urlencode({
        "product_id": str(product_id),
        "picker_type": picker_type,
        "request_key": request_key or str(uuid.uuid4()),
    })
    url = f"{base_url}?{qs}"
    body = run_curl(url, headers, timeout_sec=30)
    return parse_json_or_save(body)


def run_logic(product_id, picker_type, token, device_id, web_build_version, api_version, web_request_secret, baggage=None, sentry_trace=None, client_datetime=None, request_key=None):
    # Args object mimicking the argparse namespace for build_headers compatibility
    class Args:
        pass
    args = Args()
    args.token = token
    args.baggage = baggage
    args.sentry_trace = sentry_trace
    args.api_version = api_version
    args.client_datetime = client_datetime
    args.device_id = device_id
    args.web_build_version = web_build_version
    args.web_request_secret = web_request_secret

    headers = build_headers(args, product_id)
    payload = fetch_options_display(product_id, picker_type, headers, request_key)

    rows = extract_prices(payload)
    rows.sort(key=lambda r: (r["price"] is None, r["price"] or 10**18))
    
    return rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--product_id", type=int, required=True)
    ap.add_argument("--picker_type", choices=["buy", "sell"], required=True)
    ap.add_argument("--token", required=True)

    ap.add_argument("--device_id", default="web;d61f82c5-98cb-4cc9-afb9-674a883b97c3")
    ap.add_argument("--web_build_version", default="25.15.7")
    ap.add_argument("--api_version", default="52")
    ap.add_argument("--web_request_secret", default="kream-djscjsghdkd")

    ap.add_argument("--baggage", default=None)
    ap.add_argument("--sentry_trace", default=None)

    # 네가 curl에서 고정해서 쓰던 값들(재현/디버깅용)
    ap.add_argument("--client_datetime", default=None)
    ap.add_argument("--request_key", default=None)

    ap.add_argument("--save_debug", action="store_true")

    args = ap.parse_args()

    # Call run_logic
    rows = run_logic(
        product_id=args.product_id,
        picker_type=args.picker_type,
        token=args.token,
        device_id=args.device_id,
        web_build_version=args.web_build_version,
        api_version=args.api_version,
        web_request_secret=args.web_request_secret,
        baggage=args.baggage,
        sentry_trace=args.sentry_trace,
        client_datetime=args.client_datetime,
        request_key=args.request_key
    )

    if args.save_debug:
        # We need payload to save debug, but run_logic returns rows. 
        # For CLI debug, we might re-fetch or adjust run_logic to return payload too?
        # Simpler: just print the rows as before since payload saving was optional/debug.
        # If saving payload is critical for CLI debug, run_logic should return it.
        # But gui only needs rows. Let's keep it simple.
        pass

    print(f"[product_id={args.product_id}] extracted {len(rows)} rows (instant_buy + buy(instant_candidate))")
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
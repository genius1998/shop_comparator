"""KREAM per-size quotes, read from the same API the product page uses.

Price rules (documented in docs/pricing-and-matching.md):
- 즉시구매가 = lowest price at which a new-condition item can be bought now.
  Candidates come from `instant_buy` actions, and from `buy` actions whose
  button says "즉시 구매".  95-point / storage-sale offers (action `buy_95`,
  `add_to_cart`, or quality != 1) are excluded.
- When several delivery types offer the size, the cheapest wins and the
  chosen delivery type is stored; all candidates are kept for audit.
- A size that only offers 구매 입찰 has no 즉시구매가 (never a guessed price).
- 즉시판매가 = price of the `instant_sell` action in the sell picker, i.e. the
  highest standing buy bid.
"""
import time
import uuid
from urllib.parse import urlencode

from shop_comparator.shops import kream_crawler
from shop_comparator.core.crawl_errors import CrawlError
from shop_comparator.shops.get_kream_price6 import extract_button_title_text

DELIVERY_LABEL = {"normal": "일반배송", "immediate": "빠른배송", "overseas": "해외배송", "express": "빠른배송", "brand": "브랜드배송"}


def _first(value):
    return value[0] if isinstance(value, list) and value else value


def _int(value):
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    return int(text) if text.isdigit() else None


def _actions(payload):
    """Yield (node, action_name, parameters) for every named action."""
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for action in node.get("actions") or []:
                if isinstance(action, dict) and action.get("type") == "named_action":
                    yield node, action.get("value"), action.get("parameters") or {}
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def option_names(payload):
    """option_key -> size text shown to the buyer.

    For ordinary products they are the same ("W240").  Brand-shipped products
    use an internal option id as the key ("761962") and show the size
    separately ("240").
    """
    names = {}
    for _, action, params in _actions(payload):
        if action == "select_option":
            key, shown = _first(params.get("option_key")), _first(params.get("option_name_display"))
            if key is not None and shown not in (None, ""):
                names[str(key)] = str(shown)
    return names


def parse_buy(payload):
    """Return {size: {instantBuy, instantBuyDelivery, bidOnly, candidates}}."""
    sizes = {}
    names = option_names(payload)
    for node, action, params in _actions(payload):
        size = _first(params.get("option_key"))
        if size is None:
            continue
        size = names.get(str(size), size)
        entry = sizes.setdefault(str(size), {"candidates": [], "bid": False})
        if action == "bid_buy":
            entry["bid"] = True
            continue
        if action not in ("instant_buy", "buy"):
            continue  # select_option, add_to_cart, buy_95, ...
        quality = str(_first(params.get("quality")) or "1")
        price = _int(_first(params.get("price")))
        title = extract_button_title_text(node)
        instant = action == "instant_buy" or "즉시 구매" in title or "instant buy" in title.lower()
        if not instant:
            entry["bid"] = True
            continue
        if price is None:
            price = _int(kream_crawler._parse_price_int(kream_crawler._extract_text_from_item(node.get("title_item"))))
        if price is None or quality != "1":
            continue
        entry["candidates"].append({
            "price": price,
            "delivery": str(_first(params.get("delivery_method")) or ""),
            "action": action,
        })
    out = {}
    for size, entry in sizes.items():
        unique = {(c["price"], c["delivery"]): c for c in entry["candidates"]}
        candidates = sorted(unique.values(), key=lambda c: (c["price"], c["delivery"]))
        best = candidates[0] if candidates else None
        out[size] = {
            "instantBuy": best["price"] if best else None,
            "instantBuyDelivery": DELIVERY_LABEL.get(best["delivery"], best["delivery"]) if best else None,
            "bidOnly": not candidates and entry["bid"],
            "candidates": candidates,
        }
    return out


def parse_sell(payload):
    """Return {size: instant sell price or None}."""
    sizes = {}
    names = option_names(payload)
    for node, action, params in _actions(payload):
        size = _first(params.get("option_key"))
        if size is None:
            continue
        size = names.get(str(size), size)
        sizes.setdefault(str(size), None)
        if action != "instant_sell":
            continue
        price = _int(_first(params.get("price")))
        if price is None:
            price = kream_crawler._parse_price_int(kream_crawler._extract_text_from_item(node.get("title_item")))
        if price is not None and (sizes[str(size)] is None or price > sizes[str(size)]):
            sizes[str(size)] = price
    return sizes


def merge(buy, sell):
    quotes = []
    for size in dict.fromkeys(list(buy) + list(sell)):
        b = buy.get(size) or {}
        quotes.append({
            "size": size, "instantBuy": b.get("instantBuy"), "instantBuyDelivery": b.get("instantBuyDelivery"),
            "instantSell": sell.get(size), "bidOnly": bool(b.get("bidOnly")), "candidates": b.get("candidates") or [],
        })
    return quotes


def _header_lines(product_id, token):
    lines = [
        "accept: */*",
        "accept-language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        f"referer: https://kream.co.kr/products/{product_id}",
        f"x-kream-api-version: {kream_crawler.KREAM_API_VERSION}",
        f"x-kream-client-datetime: {kream_crawler.kst_ts()}",
        f"x-kream-device-id: {kream_crawler.KREAM_DEVICE_ID}",
        f"x-kream-web-build-version: {kream_crawler.KREAM_WEB_BUILD_VER}",
        f"x-kream-web-request-secret: {kream_crawler.KREAM_WEB_REQUEST_SEC}",
    ]
    if token:
        lines.append(f"authorization: Bearer {token}")
    return lines


def fetch_display(product_id, picker_type, token=None):
    """One options/display call through the active KREAM browser transport.

    Per-size quotes are public: no token is needed, and a stale stored token
    is never sent.  `token` is only the live session token, when logged in.
    """
    query = urlencode({"product_id": str(product_id), "picker_type": picker_type, "request_key": str(uuid.uuid4())})
    data = kream_crawler.run_curl_json(f"https://api.kream.co.kr/api/p/options/display?{query}",
                                       _header_lines(product_id, token), timeout_sec=20)
    if not isinstance(data, dict) or "content" not in data:
        raise CrawlError("KREAM", "schema", "옵션 가격 응답 형식이 예상과 다릅니다.")
    return data


def fetch_quotes(product_id, token=None):
    buy = parse_buy(fetch_display(product_id, "buy", token))
    if not buy:
        raise CrawlError("KREAM", "schema", "옵션 가격 응답에서 사이즈를 찾지 못했습니다.")
    try:
        sell = parse_sell(fetch_display(product_id, "sell", token))
    except CrawlError as exc:
        # Brand-shipped products (product_type "product_brand") cannot be resold on
        # KREAM: the sell picker answers HTTP 400.  They still have buy prices.
        if exc.code != "http":
            raise
        sell = {}
    return merge(buy, sell)


def fetch_sales(product_id, token=None, pages=2):
    """Recent sales (for the GUI's 거래량 필터). Returns {"total", "items"}."""
    items, total = [], None
    for page in range(1, pages + 1):
        query = urlencode({"cursor": str(page), "per_page": "50", "sort": "date_created[desc]",
                           "request_key": str(uuid.uuid4())}, safe="[]")
        try:
            data = kream_crawler.run_curl_json(f"https://api.kream.co.kr/api/p/products/{product_id}/sales?{query}",
                                               _header_lines(product_id, token), timeout_sec=20)
        except CrawlError as exc:
            if page > 1 and exc.code == "http":
                break
            raise
        time.sleep(1.2)
        if total is None:
            total = int(data.get("total") or 0)
        page_items = data.get("items") or []
        items.extend(page_items)
        if not page_items:
            break
    return {"total": total or 0, "items": items}

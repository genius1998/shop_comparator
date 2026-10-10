"""Independent collection stages used by the web app.

Each stage can run on its own and only touches its own shop's data:

    collect_musinsa -> products, sizes, stock and per-size member prices
    match_kream     -> Musinsa product -> KREAM product (search + verification)
    collect_kream   -> per-size KREAM quotes for matched products
    compare         -> summary of the stored comparison (no network)
    kream_filter    -> GUI "크림 필터링": drop low-volume products from the list
    collect_poizon  -> GUI "포이즌 가격 추출 (Seller)"
    full            -> the stages in order; a failed stage does not undo earlier ones

A stage stops at once on auth, block and rate-limit errors (no retry loops)
and keeps whatever was stored before.
"""
import csv
import os
import re
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlencode, urlparse

from shop_comparator.core import app_logging as L
from shop_comparator.services import comparison_service as cmp
from shop_comparator.core import config
from shop_comparator.shops import crawler
from shop_comparator.storage import database
from shop_comparator.shops import kream_api
from shop_comparator.shops import kream_crawler
from shop_comparator.shops import site_login
from shop_comparator.storage import store
from shop_comparator.core import webhook
from shop_comparator.shops.browser_transport import browser_transport
from shop_comparator.core.crawl_errors import CrawlError, response_json
from shop_comparator.core.utils import get_persistent_path

CATEGORIES = {
    "001": "상의", "002": "아우터", "003": "바지", "004": "가방", "103": "신발",
    "100": "원피스/스커트", "101": "패션소품", "026": "속옷/홈웨어", "017": "스포츠/레저",
    "102": "디지털/라이프", "104": "뷰티", "106": "키즈", "107": "아울렛",
}
RANKINGS = {
    "shoes_ranking": ("신발 랭킹", "SHOES_RANKING_LAYOUT"),
    "boutique_wallet_bag_ranking": ("부티크 - 지갑/가방", "BOUTIQUE_WALLET_BAG_RANKING_LAYOUT"),
    "boutique_accessory_ranking": ("부티크 - 악세서리", "BOUTIQUE_ACCESSORY_RANKING_LAYOUT"),
}
PRICE_MODES = {"per_option", "grouped", "none"}
STOP_CODES = {"auth", "blocked", "rate_limit", "busy"}
KREAM_BLACKLIST = "kream_search_blacklist.csv"
MAX_CONSECUTIVE_FAILURES = 5
REQUEST_GAP = 0.4


# ------------------------------------------------------------ Musinsa
@contextmanager
def musinsa_session(need_login, ctx=None):
    """Open the Musinsa Chrome profile; verify the member session if needed."""
    for attempt in (1, 2):
        relogin = False
        with browser_transport("musinsa", cookies=site_login.musinsa_saved_cookies()) as browser:
            payload = {"cookies": browser.cookies(),
                       "user_agent": browser.driver.execute_script("return navigator.userAgent")}
            crawler.set_auth_payload(payload)
            session = crawler.build_session("", payload)
            if need_login:
                ok, detail = site_login.verify_musinsa(browser)
                if not ok:
                    site_login.mark_expired("musinsa", detail)
                    relogin = attempt == 1 and site_login.vault.has("musinsa")
                    if not relogin:
                        raise CrawlError("무신사", "auth", "무신사 로그인이 필요합니다. 로그인 탭에서 다시 로그인해 주세요. (" + detail + ")")
                else:
                    store.save_session("musinsa", "logged_in", None, detail, verified=True)
            if not relogin:
                yield session, payload
                return
        if not site_login.relogin_if_possible("musinsa", ctx.cancel if ctx else None):
            raise CrawlError("무신사", "auth", "무신사 세션이 만료되었고 자동 재로그인에 실패했습니다. 로그인 탭에서 다시 로그인해 주세요.")


def _ranking_ids(session, layout_url, limit):
    section = re.search(r"sectionId=(\d+)", layout_url)
    if not section:
        raise CrawlError("무신사", "schema", "랭킹 주소에서 sectionId를 찾지 못했습니다.")
    params = {k: v[0] for k, v in parse_qs(urlparse(layout_url).query, keep_blank_values=True).items()}
    found, page = [], 1
    while len(found) < limit:
        params.update(page=page, startRank=len(found) + 1, offset=len(found))
        url = f"https://api.musinsa.com/api2/hm/web/v5/pans/ranking/sections/{section[1]}?" + urlencode(params)
        items = crawler._pick_items_from_sections_api(response_json(session.get(url, timeout=20)))
        fresh = [str(item[1]) for item in sorted(items, key=lambda t: t[0]) if str(item[1]) not in found]
        if not fresh:
            break
        found.extend(dict.fromkeys(fresh))
        page += 1
        time.sleep(0.3)
    return found[:limit]


def discover(session, settings, ctx):
    """Return [(goodsNo, rank, meta-or-None)] for the requested source."""
    mode, limit = settings["mode"], settings["limit"]
    out, seen = [], set()

    def add(goods_no, rank=None, meta=None):
        goods_no = str(goods_no)
        if goods_no and goods_no != "None" and goods_no not in seen:
            seen.add(goods_no)
            out.append((goods_no, rank, meta))

    if mode in ("products", "selected"):
        for goods_no in settings["ids"]:
            add(goods_no)
    elif mode == "current":
        for product in store.products():
            add(product["goodsNo"], product.get("rank"))
    elif mode == "ranking":
        for code in settings["rankings"]:
            ctx.check()
            label, attr = RANKINGS[code]
            L.log(f"{label} 상위 {limit}개 목록을 조회합니다.")
            for index, goods_no in enumerate(_ranking_ids(session, getattr(config, attr), limit), 1):
                add(goods_no, index)
    elif mode == "category":
        for code in settings["categories"]:
            ctx.check()
            L.log(f"카테고리 {CATEGORIES[code]} 인기순 {limit}개 목록을 조회합니다.")
            for rank, goods_no, _ in crawler.fetch_category_items(session, [code], limit, strict=True):
                add(goods_no, rank)
    elif mode == "brand":
        for brand in settings["brands"]:
            ctx.check()
            products = crawler.get_products_by_brand(brand, limit, log_callback=L.callback())
            if not products:
                L.warn(f"브랜드 '{brand}'에서 상품을 찾지 못했습니다.")
            for index, meta in enumerate(products, 1):
                if meta.get("thumbnail") and "imageUrl" not in meta:
                    meta["imageUrl"] = meta["thumbnail"]
                add(meta.get("goodsNo"), meta.get("rank") or index, meta)
    if not out:
        raise CrawlError("무신사", "schema", "수집할 상품을 찾지 못했습니다. 입력값이나 접속 상태를 확인해 주세요.")
    return out


def variant_size(item, size_option_nos):
    """Display size of one option (same rule as crawler.extract_stock_size_sets)."""
    managed = str(item.get("managedCode") or "")
    for value in item.get("optionValues") or []:
        if str(value.get("optionNo")) in size_option_nos:
            return str(value.get("code") or value.get("name") or "")
    if not managed:
        return str((item.get("optionValues") or [{}])[0].get("name") or "")
    return managed.split("^")[-1] if "^" in managed else managed


def _option_rows(opts):
    data = opts.get("data") or {}
    size_nos = {str(b.get("no")) for b in data.get("basic") or []
                if b.get("standardOptionNo") == 6 or "사이즈" in (b.get("name") or "") or "size" in (b.get("name") or "").lower()}
    rows = []
    for item in data.get("optionItems") or []:
        if item.get("isDeleted") or item.get("isRedirect"):
            continue
        option_no = str(item.get("no") or "")
        size = variant_size(item, size_nos)
        if not option_no or not size:
            raise CrawlError("무신사", "schema", "옵션 번호 또는 사이즈 이름이 누락되었습니다.")
        rows.append({
            "optionNo": option_no, "size": size, "managedCode": str(item.get("managedCode") or ""),
            "soldout": 1 if (item.get("isSoldOut") or item.get("outOfStock")) else 0,
            "remainQuantity": item.get("remainQuantity"), "optionPrice": item.get("price"),
        })
    if not rows:
        raise CrawlError("무신사", "schema", "사이즈 정보가 비어 있습니다.")
    return rows


def _price_fields(priced, source):
    return {k: priced.get(k) for k in ("salePrice", "couponName", "couponValueKind", "couponDiscount",
                                       "memberDiscountRate", "memberDiscountAmount", "pointDiscountAmount", "finalPrice")} | {
        "priceStatus": "verified", "priceSource": source, "error": None, "errorKind": None, "checkedAt": store.now()}


def collect_one(session, payload, goods_no, rank, meta, price_mode, ctx, failures):
    """Collect one product. Returns 'ok', 'partial' or raises on stop-worthy errors."""
    if not meta:
        meta = crawler.fetch_details(session, [goods_no], sleep=0).get(goods_no)
    if not meta:
        raise CrawlError("무신사", "schema", "상품 상세 정보를 찾지 못했습니다(판매 종료 또는 잘못된 상품번호).")
    opts = crawler.fetch_options_any(session, goods_no)
    rows = _option_rows(opts)
    known = store.product(goods_no) or {}
    serial = known.get("serial_number") or crawler._fetch_serial_number_robust(session, goods_no)
    stamp = store.now()
    price_groups, first_priced, problems = {}, None, 0
    unorderable = None  # the shop refuses to order this product at all
    for row in rows:
        ctx.check()
        values = {**row, "stockCheckedAt": stamp}
        attempted = False
        if row["soldout"]:
            values.update(priceStatus="soldout", error=None, errorKind=None)
        elif unorderable and price_mode != "none":
            # Same product-level refusal applies to every size: do not repeat the request.
            attempted = True
            problems += 1
            values.update(priceStatus="error", error=unorderable, errorKind="unorderable")
        elif price_mode == "none":
            values.update(priceStatus="not_requested")
        else:
            group = row["optionPrice"] if price_mode == "grouped" and row["optionPrice"] is not None else None
            if group is not None and group in price_groups:
                source_no, priced = price_groups[group]
                values.update(_price_fields(priced, f"주문서(옵션 추가금이 같은 대표 옵션 {source_no} 값)"))
            else:
                attempted = True
                L.log(f"사이즈 {row['size']} 회원가 조회(장바구니 → 주문서 → 쿠폰)", stage="musinsa-price")
                try:
                    priced = crawler.price_option("", goods_no, row["optionNo"], auth_payload=payload, sleep_each=REQUEST_GAP)
                    values.update(_price_fields(priced, "주문서(해당 옵션)"))
                    values["_priced"] = priced
                    if group is not None:
                        price_groups[group] = (row["optionNo"], priced)
                    failures[0] = 0
                except CrawlError as exc:
                    if exc.code in STOP_CODES:
                        store.save_variant(goods_no, row["optionNo"], {**values, "priceStatus": "error", "error": str(exc), "errorKind": exc.code})
                        raise
                    problems += 1
                    if exc.code == "api" and "[ORDER-" in str(exc):
                        # e.g. "판매가 종료된 상품", "구매 불가능한 상품": a property of this
                        # product, not a sign that the session or the site is failing.
                        unorderable = str(exc)
                        values.update(priceStatus="error", error=unorderable, errorKind="unorderable")
                        L.warn(f"이 상품은 무신사가 주문을 받지 않아 회원가를 구할 수 없습니다: {exc}", stage="musinsa-price")
                    else:
                        failures[0] += 1
                        values.update(priceStatus="error", error=str(exc), errorKind=exc.code)
                        L.error(f"사이즈 {row['size']} 회원가 확인 실패: {exc}", exc.code, stage="musinsa-price")
                except Exception as exc:
                    problems += 1
                    failures[0] += 1
                    values.update(priceStatus="error", error=cmp.safe_error(exc), errorKind="internal")
                    L.error(f"사이즈 {row['size']} 회원가 확인 실패: {type(exc).__name__}", "internal", stage="musinsa-price")
        priced = values.pop("_priced", None)
        if priced and first_priced is None:
            first_priced = (row, priced)
        store.save_variant(goods_no, row["optionNo"], values, price_attempted=attempted)
        if failures[0] >= MAX_CONSECUTIVE_FAILURES:
            raise CrawlError("무신사", "pricing", f"회원가 조회가 {MAX_CONSECUTIVE_FAILURES}회 연속 실패하여 중단했습니다. 로그인 상태와 오류 로그를 확인해 주세요.")
    store.mark_missing_variants(goods_no, [r["optionNo"] for r in rows])
    available = [r["size"] for r in rows if not r["soldout"]]
    product = {
        "goodsNo": goods_no, "rank": rank if rank is not None else known.get("rank"),
        "linkUrl": meta.get("linkUrl") or config.PRODUCT_URL.format(goods_no=goods_no),
        "goodsName": meta.get("goodsName"), "brandName": meta.get("brandName"), "serial_number": serial,
        "imageUrl": meta.get("imageUrl"), "normalPrice": meta.get("normalPrice"), "saleRate": meta.get("saleRate"),
        "availableSizes": ",".join(available), "soldout": not available,
    }
    if first_priced:  # GUI-compatible product-level columns: first purchasable option
        row, priced = first_priced
        product.update({k: priced.get(k) for k in (
            "salePrice", "couponDiscount", "finalPrice", "memberDiscountAmount", "pointDiscountAmount",
            "memberDiscountRate", "memberSavePointRate", "isMemberDiscount", "isUsePointDiscount",
            "goodsSavePoint", "couponName", "couponValueKind", "baseCategoryFullName")})
        product.update(managedCode=row["managedCode"], goodsOptionNo=row["optionNo"])
    store.upsert_product(product, source="web")
    return "partial" if problems else "ok"


def collect_musinsa(settings, ctx):
    price_mode = settings.get("price_mode", "grouped")
    with L.bind(shop="musinsa", stage="musinsa"):
        with musinsa_session(need_login=price_mode != "none", ctx=ctx) as (session, payload):
            ctx.progress("무신사 목록 조회", 0, 0)
            targets = discover(session, settings, ctx)
            for goods_no, kream_id in (settings.get("kream_ids") or {}).items():
                store.set_manual_match(goods_no, kream_id, "수집 입력에서 직접 지정")
            metas = {}
            missing = [g for g, _, meta in targets if not meta]
            if missing:
                metas = crawler.fetch_details(session, missing, sleep=0.3)
            if settings.get("replace_list"):
                keep = {g for g, _, _ in targets}
                store.set_active([p["goodsNo"] for p in store.products() if str(p["goodsNo"]) not in keep], False)
                L.log("기존 목록의 다른 상품은 숨김 처리했습니다(데이터는 보존).")
            ctx.note("targets", len(targets))
            # Recorded up front so the products tab shows this batch while it is being collected.
            if not settings.get("keep_batch"):
                store.set_setting("last_collection", {"goods": [g for g, _, _ in targets], "at": store.now()})
            store.plan_items(ctx.id, "musinsa", [g for g, _, _ in targets])
            ctx.note("priceMode", price_mode)
            failures = [0]
            for index, (goods_no, rank, meta) in enumerate(targets):
                ctx.check()
                ctx.progress("무신사 수집", index, len(targets))
                with L.bind(goods=goods_no):
                    L.log(f"({index + 1}/{len(targets)}) 무신사 상품 조회")
                    try:
                        result = collect_one(session, payload, goods_no, rank, meta or metas.get(goods_no), price_mode, ctx, failures)
                        store.set_state(goods_no, "musinsa", result, None if result == "ok" else "일부 사이즈 회원가 확인 실패")
                        store.mark_item(ctx.id, "musinsa", goods_no, "ok" if result == "ok" else "failed",
                                        None if result == "ok" else "partial", None if result == "ok" else "일부 사이즈 회원가 확인 실패")
                        ctx.count("ok" if result == "ok" else "failed")
                    except CrawlError as exc:
                        if exc.code == "auth":
                            site_login.mark_expired("musinsa", str(exc))
                        if store.product(goods_no):
                            store.set_state(goods_no, "musinsa", "error", str(exc))
                        store.mark_item(ctx.id, "musinsa", goods_no, "failed", exc.code, L.redact(str(exc)))
                        if exc.code in STOP_CODES or exc.code == "pricing" and failures[0] >= MAX_CONSECUTIVE_FAILURES:
                            raise
                        ctx.count("failed")
                        L.error(f"상품 수집 실패: {exc}", exc.code)
                ctx.wait(REQUEST_GAP)
            ctx.progress("무신사 수집", len(targets), len(targets))
            ctx.note("goods", [g for g, _, _ in targets])
            # "KREAM 매칭 / KREAM 가격 / 비교" default to exactly this batch.
            if not settings.get("keep_batch"):
                store.set_setting("last_collection", {"goods": [g for g, _, _ in targets], "at": store.now()})
    return [g for g, _, _ in targets]


# -------------------------------------------------------------- KREAM
@contextmanager
def kream_session(ctx=None, need_login=False):
    """Open the KREAM Chrome profile.

    Search and per-size quotes are public, so matching and price collection
    run without a login.  Sales history (거래량 필터) needs a member session;
    with need_login=True the session is verified and re-login is attempted.
    """
    for attempt in (1, 2):
        relogin = False
        with browser_transport("kream", start_url=f"https://kream.co.kr/products/{site_login.KREAM_VERIFY_PRODUCT}") as browser:
            time.sleep(2)
            token = site_login.save_kream_session(browser, site_login.kream_member_token(browser) if need_login else None)
            if token or need_login:
                ok, detail = site_login.verify_kream(browser, token)
                if ok:
                    store.save_session("kream", "logged_in", None, detail, verified=True)
                else:
                    site_login.mark_expired("kream", detail)
                    kream_crawler.set_session_token(None)  # never send a rejected token
                    token = None
                    relogin = need_login and attempt == 1 and site_login.vault.has("kream")
                    if need_login and not relogin:
                        raise CrawlError("KREAM", "auth", "이 작업은 KREAM 로그인이 필요합니다. 로그인 탭에서 로그인해 주세요. (" + detail + ")")
            if not relogin:
                yield browser, token
                return
        if not site_login.relogin_if_possible("kream", ctx.cancel if ctx else None):
            raise CrawlError("KREAM", "auth", "KREAM 세션이 만료되었고 자동 재로그인에 실패했습니다. 로그인 탭에서 다시 로그인해 주세요.")


def _blacklist_path():
    return get_persistent_path(KREAM_BLACKLIST)


def _blacklist():
    path = _blacklist_path()
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return {row.get("musinsa_goodsNo", "").strip() for row in csv.DictReader(f)} - {""}
    except OSError:
        return set()


def _blacklist_add(goods_no):
    path = _blacklist_path()
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(["musinsa_goodsNo"])
        writer.writerow([goods_no])


def blacklist_remove(goods_nos):
    """User action from the 매칭 tab: allow these products to be searched again."""
    drop = {str(g) for g in goods_nos}
    path = _blacklist_path()
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
    except OSError:
        return
    kept = [row for row in rows if not row or row[0].strip() not in drop]
    if len(kept) == len(rows):
        return  # nothing to remove: leave the user's file untouched
    with open(path, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(kept)


def blacklist():
    return sorted(_blacklist())


def _targets(goods_nos):
    products = store.products(active_only=goods_nos is None, goods_nos=goods_nos)
    if not products:
        raise CrawlError("KREAM", "prerequisite", "무신사 상품 목록이 비어 있습니다. 먼저 [무신사만 수집]을 실행해 주세요.")
    return products


def match_products(products, ctx, force=False):
    """Search KREAM for each product lacking a verified match (needs kream_session)."""
    skip = set() if force else _blacklist()
    for index, product in enumerate(products):
        ctx.check()
        ctx.progress("KREAM 매칭", index, len(products))
        goods_no = str(product["goodsNo"])
        with L.bind(goods=goods_no, stage="kream-match"):
            info = cmp.match_info(store.match_for(goods_no))
            if info["usable"] and not force:
                ctx.count("alreadyMatched")
                continue
            if goods_no in skip:
                ctx.count("blacklisted")
                L.log("검색 제외 목록에 있어 건너뜁니다(매칭 탭에서 해제 가능).")
                continue
            if not product.get("goodsName"):
                ctx.count("failed")
                continue
            try:
                found = kream_crawler.search_product(product["goodsName"], product.get("brandName"),
                                                     product.get("serial_number"), None, image_url=product.get("imageUrl"))
            except CrawlError as exc:
                if exc.code in STOP_CODES:
                    raise
                ctx.count("failed")
                L.error(f"KREAM 검색 실패: {exc}", exc.code)
                continue
            if not found:
                ctx.count("notFound")
                _blacklist_add(goods_no)
                L.warn("검증을 통과한 KREAM 상품을 찾지 못해 검색 제외 목록에 추가했습니다.")
                continue
            if database.is_kream_id_already_used(str(int(found)), goods_no, verified_only=True):
                ctx.count("duplicate")
                L.warn(f"KREAM 상품 {int(found)}은 이미 다른 무신사 상품에 연결되어 있어 건너뜁니다.")
                continue
            url = f"https://kream.co.kr/products/{int(found)}"
            database.cache_kream_search(goods_no, found, url)
            database.update_product_kream_id(goods_no, str(int(found)))
            database.update_product_kream_url(goods_no, url)
            ctx.count("matched")
            L.log(f"KREAM 상품 {int(found)} 매칭 (신뢰도 {getattr(found, 'match_confidence', None)}, 방식 {getattr(found, 'match_method', None)})")
    ctx.progress("KREAM 매칭", len(products), len(products))


def match_kream(goods_nos, ctx, force=False):
    with L.bind(shop="kream", stage="kream-match"):
        products = _targets(goods_nos)
        with kream_session(ctx):
            match_products(products, ctx, force)


ALERT_BASES = {
    "kream_buy_minus_musinsa": "KREAM 즉시구매가 − 무신사 회원가",
    "kream_sell_minus_musinsa": "KREAM 즉시판매가 − 무신사 회원가",
    "musinsa_minus_kream_buy": "무신사 회원가 − KREAM 즉시구매가 (기존 GUI 규칙)",
}


def alert_difference(basis, price, buy, sell):
    """Price gap used for the Discord alert, or None when a side is missing."""
    if basis == "kream_sell_minus_musinsa":
        return None if sell is None else sell - price
    if buy is None:
        return None
    return price - buy if basis == "musinsa_minus_kream_buy" else buy - price


def _notify_margin(product, kream_id, quotes):
    """Discord alert for sizes whose price gap reaches the alert margin.

    The gap is chosen in settings (ALERT_BASES).  The desktop GUI only had
    "Musinsa minus KREAM instant-buy"; that rule is still selectable.
    """
    url = store.get_setting("webhook_url")
    if not url:
        return
    margin = int(store.get_setting("alert_margin", 0) or 0)
    basis = store.get_setting("alert_basis", "kream_buy_minus_musinsa")
    if basis not in ALERT_BASES:
        basis = "kream_buy_minus_musinsa"
    by_key = {}
    for q in quotes:
        by_key.setdefault(cmp.size_key(q["size"]), []).append(q)
    for variant in store.variants(product["goodsNo"]):
        price = variant.get("finalPrice") if variant.get("priceStatus") == "verified" else None
        candidates = by_key.get(cmp.size_key(variant.get("size")), [])
        if price is None or len(candidates) != 1:
            continue
        buy, sell = candidates[0].get("instantBuy"), candidates[0].get("instantSell")
        diff = alert_difference(basis, price, buy, sell)
        if diff is None or diff <= 0 or diff < margin:
            continue
        message = (
            f"**수익 사이즈 감지**\n\n**브랜드:** {product.get('brandName')}\n**상품명:** {product.get('goodsName')}\n"
            f"**사이즈:** {variant.get('size')}\n**무신사 최종가:** {price:,}원\n"
            f"**크림 즉시구매가:** {f'{buy:,}원' if buy is not None else '-'}\n"
            f"**크림 즉시판매가:** {f'{sell:,}원' if sell is not None else '-'}\n**차액({ALERT_BASES[basis]}):** {diff:,}원\n"
            f"**알림 마진 기준:** {margin:,}원\n**무신사 링크:** https://www.musinsa.com/products/{product['goodsNo']}\n"
            f"**크림 링크:** https://kream.co.kr/products/{kream_id}")
        sent = webhook.send_discord_notification(url, message)
        L.log(f"디스코드 알림 {'전송' if sent else '전송 실패'}: 사이즈 {variant.get('size')}", stage="notify",
              level="info" if sent else "warning")


def quote_products(products, token, ctx):
    """Fetch quotes for matched products (needs kream_session)."""
    fetched = {}
    network_failures = 0
    store.plan_items(ctx.id, "kream", [p["goodsNo"] for p in products])
    for index, product in enumerate(products):
        ctx.check()
        ctx.progress("KREAM 가격 수집", index, len(products))
        goods_no = str(product["goodsNo"])
        with L.bind(goods=goods_no, stage="kream-price"):
            info = cmp.match_info(store.match_for(goods_no))
            if not info["kreamId"]:
                ctx.count("unmatched")
                store.mark_item(ctx.id, "kream", goods_no, "skipped", "unmatched", "매칭된 KREAM 상품이 없습니다.")
                continue
            if not info["usable"]:
                ctx.count("unverified")
                store.mark_item(ctx.id, "kream", goods_no, "skipped", "unverified", "미검증 매칭이라 가격을 수집하지 않았습니다.")
                L.warn(f"KREAM 상품 {info['kreamId']} 매칭이 미검증 상태라 가격을 수집하지 않습니다(매칭 탭에서 승인).")
                continue
            kream_id = info["kreamId"]
            try:
                if kream_id not in fetched:
                    try:
                        fetched[kream_id] = kream_api.fetch_quotes(kream_id, token)
                    except CrawlError as exc:
                        if exc.code != "network":
                            raise
                        # A dropped connection fails instantly; without a pause the loop
                        # would burn through the rest of the list in seconds.
                        L.warn("KREAM 연결이 끊겨 5초 뒤 한 번 다시 시도합니다.")
                        ctx.wait(5)
                        fetched[kream_id] = kream_api.fetch_quotes(kream_id, token)
                    network_failures = 0
                    store.save_kream_quotes(kream_id, fetched[kream_id])
                    database.save_kream_size_prices(kream_id, {q["size"]: q["instantBuy"] for q in fetched[kream_id]})
                    database.save_kream_size_prices_v2(kream_id, {q["size"]: q["instantSell"] for q in fetched[kream_id]})
                    ctx.wait(1.0)
                quotes = fetched[kream_id]
                store.set_state(goods_no, "kream", "ok")
                store.mark_item(ctx.id, "kream", goods_no, "ok")
                ctx.count("ok")
                L.log(f"KREAM {kream_id}: 사이즈 {len(quotes)}개, 즉시구매가 {sum(q['instantBuy'] is not None for q in quotes)}개, "
                      f"즉시판매가 {sum(q['instantSell'] is not None for q in quotes)}개")
                _notify_margin(product, kream_id, quotes)
            except CrawlError as exc:
                store.set_state(goods_no, "kream", "error", str(exc))  # earlier quotes stay in place
                store.mark_item(ctx.id, "kream", goods_no, "failed", exc.code, L.redact(str(exc)))
                if exc.code == "auth":
                    site_login.mark_expired("kream", str(exc))
                if exc.code in STOP_CODES:
                    raise
                ctx.count("failed")
                L.error(f"KREAM 가격 조회 실패: {exc}", exc.code)
                if exc.code == "network":
                    network_failures += 1
                    if network_failures >= 3:
                        raise CrawlError("KREAM", "network", "KREAM 연결 실패가 3회 연속 발생해 중단했습니다. 잠시 뒤 다시 실행해 주세요(수집된 결과는 유지됩니다).")
                ctx.wait(2)
    ctx.progress("KREAM 가격 수집", len(products), len(products))


def collect_kream(goods_nos, ctx, match_missing=False):
    with L.bind(shop="kream", stage="kream-price"):
        products = _targets(goods_nos)
        if not match_missing and not any(cmp.match_info(store.match_for(p["goodsNo"]))["usable"] for p in products):
            raise CrawlError("KREAM", "prerequisite", "KREAM과 매칭된 상품이 없습니다. 먼저 [KREAM 매칭]을 실행하거나 매칭 탭에서 직접 지정해 주세요.")
        with kream_session(ctx) as (_, token):
            if match_missing:
                match_products(products, ctx)
            quote_products(products, token, ctx)


RESUMABLE = {"musinsa": "무신사 수집", "kream": "KREAM 가격 수집", "vlm": "VLM 검토"}


def unfinished(job_id):
    """{stage: [goodsNo, ...]} of the targets a job did not finish: failed, or never reached."""
    out = {}
    for row in store.job_items(job_id):
        if row["stage"] in RESUMABLE and row["state"] in ("pending", "failed"):
            out.setdefault(row["stage"], []).append(row["goodsNo"])
    return out


def resume_musinsa(goods_nos, price_mode, ctx):
    """Collect only the given products again.  Rows are upserted, so nothing is duplicated."""
    settings = {"mode": "selected", "ids": [str(g) for g in goods_nos], "limit": len(goods_nos), "price_mode": price_mode,
                "keep_batch": True}      # a retry must not shrink "the batch collected last" to the retried products
    return collect_musinsa(settings, ctx)


def compare(goods_nos, ctx):
    with L.bind(stage="compare"):
        cmp.require_products(goods_nos)
        rows = cmp.build(goods_nos)
        if not any(r["size"] for r in rows):
            raise CrawlError("비교", "prerequisite", "수집된 무신사 사이즈가 없습니다. 먼저 [무신사만 수집]을 실행해 주세요.")
        summary = cmp.summarize(rows)
        for key, value in summary.items():
            ctx.note(key, value)
        L.log(f"비교 결과: 상품 {summary['products']}개, 사이즈 {summary['rows']}행, 비교 완료 {summary['compared']}행 · {summary['byStatus']}")
        if not any(r["kreamBuy"] is not None for r in rows):
            L.warn("KREAM 가격이 아직 없어 가격 차이를 계산한 행이 없습니다. [KREAM 가격 수집]을 실행해 주세요.")


def kream_filter(goods_nos, min_total, min_monthly, ctx):
    """GUI 크림 필터링: record sales counts and hide products under the thresholds."""
    from datetime import datetime, timedelta, timezone
    with L.bind(shop="kream", stage="kream-filter"):
        products = _targets(goods_nos)
        month_ago = datetime.now(timezone.utc) - timedelta(days=30)
        with kream_session(ctx, need_login=True) as (_, token):
            for index, product in enumerate(products):
                ctx.check()
                ctx.progress("거래량 필터", index, len(products))
                goods_no = str(product["goodsNo"])
                with L.bind(goods=goods_no):
                    info = cmp.match_info(store.match_for(goods_no))
                    if not info["kreamId"]:
                        ctx.count("unmatched")
                        continue
                    try:
                        sales = kream_api.fetch_sales(info["kreamId"], token)
                    except CrawlError as exc:
                        if exc.code in STOP_CODES:
                            raise
                        ctx.count("failed")
                        L.error(f"체결 내역 조회 실패: {exc}", exc.code)
                        continue
                    monthly = 0
                    for item in sales["items"]:
                        try:
                            when = datetime.strptime(item.get("date_created"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                        except (TypeError, ValueError):
                            continue
                        monthly += when >= month_ago
                    database.update_product_kream_transactions(goods_no, sales["total"], monthly)
                    if sales["total"] < min_total or monthly < min_monthly:
                        store.set_active([goods_no], False)
                        ctx.count("hidden")
                        L.log(f"거래 {sales['total']}건(최근 30일 {monthly}건)으로 기준 미달: 목록에서 숨김")
                    else:
                        ctx.count("kept")
                        L.log(f"거래 {sales['total']}건(최근 30일 {monthly}건): 유지")
                    ctx.wait(3.0)  # the sales API answered 429 after ~30 products at 0.8 s spacing


# ------------------------------------------------------------- POIZON
def collect_poizon(goods_nos, account, password, ctx):
    """Port of gui.App._process_poizon_task('CRAWL_LIST')."""
    import pandas as pd
    from shop_comparator.shops import poizon_seller_crawler
    with L.bind(shop="poizon", stage="poizon"):
        products = _targets(goods_nos)
        cookies_path = get_persistent_path("poizon_cookies.json")
        cache_file = get_persistent_path("poizon_musinsa_overlapping_products.csv")
        blacklist_file = get_persistent_path("poizon_search_blacklist.csv")
        if password:
            L.register_secret(password)
        crawl = poizon_seller_crawler.PoizonSellerCrawler(log_callback=L.callback())
        try:
            crawl.start_browser(headless=True, cookies_path=cookies_path)
            if not crawl.login(account, password, cookies_path=cookies_path):
                store.save_session("poizon", "failed", L.mask_account(account) or None, "POIZON 로그인 실패")
                raise CrawlError("POIZON", "auth", "POIZON 로그인에 실패했습니다. 아이디/비밀번호를 확인해 주세요.")
            store.save_session("poizon", "logged_in", L.mask_account(account) or None, "셀러 센터 로그인 확인", verified=True)
            cache = {}
            if os.path.exists(cache_file):
                frame = pd.read_csv(cache_file, encoding="utf-8-sig")
                for _, row in frame.iterrows():
                    link = row.get("poizon_link") if "poizon_link" in frame.columns else None
                    if not (isinstance(link, str) and link):
                        link = row.get("poizon_url") if "poizon_url" in frame.columns else None
                    if isinstance(link, str) and link:
                        cache[str(row.get("musinsa_product_id", ""))] = (link, str(row.get("musinsa_serial_number", "") or "").strip())
            skip = set()
            if os.path.exists(blacklist_file):
                with open(blacklist_file, encoding="utf-8") as f:
                    skip = {row[0].strip() for row in csv.reader(f) if row}
            new_entries = []
            for index, product in enumerate(products):
                ctx.check()
                ctx.progress("POIZON 가격 수집", index, len(products))
                goods_no = str(product["goodsNo"])
                with L.bind(goods=goods_no):
                    if goods_no in skip:
                        ctx.count("blacklisted")
                        continue
                    name = (product.get("goodsName") or "").strip()
                    serial = str(product.get("serial_number") or "").strip()
                    if not serial and "/" in name and len(name.rsplit("/", 1)[-1].strip()) >= 3:
                        serial = name.rsplit("/", 1)[-1].strip()
                    if not serial:
                        m = re.search(r"\(([^)]+)\)$", name)
                        serial = m.group(1).strip() if m and len(m.group(1).strip()) >= 3 else ""
                    if not serial:
                        ctx.count("noSerial")
                        L.warn("품번이 없어 POIZON 검색을 건너뜁니다.")
                        continue
                    try:
                        prices, link = {}, ""
                        cached = cache.get(goods_no)
                        if cached and (not cached[1] or cached[1] == serial):
                            prices, link = crawl.scrape_direct_url(cached[0])
                        if not prices:
                            prices, link = crawl.search_and_scrape(serial)
                            if link:
                                new_entries.append({"musinsa_product_id": goods_no, "musinsa_brand_name": product.get("brandName", ""),
                                                    "musinsa_product_name": name, "musinsa_serial_number": serial, "poizon_product_id": "",
                                                    "poizon_product_name": "", "poizon_url": "", "poizon_image_url": "", "poizon_link": link})
                        if prices:
                            store.save_poizon_quotes(goods_no, prices, link)
                            store.set_state(goods_no, "poizon", "ok")
                            ctx.count("ok")
                            L.log(f"POIZON 가격 {len(prices)}개 사이즈 수집")
                        else:
                            store.set_state(goods_no, "poizon", "error", "검색 결과 없음")
                            ctx.count("notFound")
                            if not link and not cached:
                                with open(blacklist_file, "a", newline="", encoding="utf-8") as f:
                                    csv.writer(f).writerow([goods_no])
                    except Exception as exc:
                        store.set_state(goods_no, "poizon", "error", cmp.safe_error(exc))
                        ctx.count("failed")
                        L.error(f"POIZON 수집 실패: {type(exc).__name__}", "internal")
                    ctx.wait(1.0)
            if new_entries:
                fields = list(new_entries[0])
                header = not os.path.exists(cache_file)
                with open(cache_file, "a", newline="", encoding="utf-8-sig") as f:
                    writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                    if header:
                        writer.writeheader()
                    writer.writerows(new_entries)
        finally:
            if password:
                L.forget_secret(password)
            try:
                crawl.close()
            except Exception:
                pass


def login_poizon(account, password, ctx):
    """Port of gui.App._process_poizon_task('LOGIN')."""
    from shop_comparator.shops import poizon_seller_crawler
    with L.bind(shop="poizon", stage="login"):
        L.register_secret(password)
        cookies_path = get_persistent_path("poizon_cookies.json")
        crawl = poizon_seller_crawler.PoizonSellerCrawler(log_callback=L.callback())
        try:
            crawl.start_browser(headless=True, cookies_path=cookies_path)
            ok = crawl.login(account, password, cookies_path=cookies_path)
        finally:
            L.forget_secret(password)
            try:
                crawl.close()
            except Exception:
                pass
        store.save_session("poizon", "logged_in" if ok else "failed", L.mask_account(account), "셀러 센터 로그인" if ok else "로그인 실패", verified=ok)
        if not ok:
            raise CrawlError("POIZON", "auth", "POIZON 로그인에 실패했습니다.")


# --------------------------------------------------------------- full
def full(settings, ctx, with_poizon=None):
    """Musinsa -> KREAM matching -> KREAM prices -> comparison, in order.

    A later stage failing never removes what an earlier stage stored.
    """
    errors = []
    goods = None
    try:
        goods = collect_musinsa(settings, ctx)
    except CrawlError as exc:
        if exc.code == "cancelled":
            raise
        errors.append(f"무신사: {exc}")
        L.error(f"무신사 단계 실패, 이미 저장된 결과로 다음 단계를 진행합니다: {exc}", exc.code)
        goods = [g for g in settings.get("ids") or []] or None
    ctx.check()
    if with_poizon:
        try:
            collect_poizon(goods, with_poizon[0], with_poizon[1], ctx)
        except CrawlError as exc:
            errors.append(f"POIZON: {exc}")
            L.error(f"POIZON 단계 실패: {exc}", exc.code)
    else:
        try:
            collect_kream(goods, ctx, match_missing=True)
        except CrawlError as exc:
            if exc.code == "cancelled":
                raise
            errors.append(f"KREAM: {exc}")
            L.error(f"KREAM 단계 실패, 무신사 결과는 유지됩니다: {exc}", exc.code)
    ctx.check()
    try:
        compare(goods, ctx)
    except CrawlError as exc:
        errors.append(str(exc))
    if errors:
        ctx.note("stageErrors", errors)

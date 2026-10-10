"""Image-based review of Musinsa <-> KREAM product matches.

For each Musinsa product this collects KREAM search candidates and saves the
Musinsa thumbnail and every candidate's thumbnail as JPEG under
match_review/images/<goodsNo>/ so that a person (or a vision model) can judge
whether they show the same product.

    python -m shop_comparator.matching.match_review collect            # last collected Musinsa batch
    python -m shop_comparator.matching.match_review collect 123 456    # specific Musinsa product numbers
    python -m shop_comparator.matching.match_review models             # read each candidate's model number from its KREAM page
    python -m shop_comparator.matching.match_review decide             # model-code decision per product
    python -m shop_comparator.matching.match_review sheets             # side-by-side contact sheets
    python -m shop_comparator.matching.match_review export             # Excel to check matches by eye (ids, names, images, links)

Nothing here writes matches, the search-exclusion list or calls any paid API.
"""
import io
import json
import os
import re
import sys
import time

import requests
from PIL import Image, ImageDraw, ImageFont

from shop_comparator.core import app_logging as L
from shop_comparator.shops import kream_crawler
from shop_comparator.storage import store
from shop_comparator.shops.browser_transport import browser_transport
from shop_comparator.core.crawl_errors import CrawlError
from shop_comparator.core.utils import get_persistent_path

ROOT = get_persistent_path("match_review")
IMAGES = os.path.join(ROOT, "images")
SHEETS = os.path.join(ROOT, "sheets")
CANDIDATES = os.path.join(ROOT, "candidates.json")
MAX_CANDIDATES = 4
THUMB = 512
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
ALLOWED_HOSTS = ("msscdn.net", "kream-phinf.pstatic.net", "kream.co.kr", "pstatic.net")


def load():
    try:
        with open(CANDIDATES, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(data):
    os.makedirs(ROOT, exist_ok=True)
    with open(CANDIDATES, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def name_codes(name):
    """Manufacturer model codes written inside a Musinsa product name.

    Some brands register an internal code as the Musinsa 품번 and put the real
    model code in the name: "NBPDGF733B / U740BM2 (BLACK)", "XA 프로 3D_L41617400".
    """
    out = []
    for token in re.split(r"[^A-Za-z0-9\-]+", str(name or "")):
        token = token.strip("-")
        if len(token) >= 6 and any(c.isdigit() for c in token) and (any(c.isalpha() for c in token) or "-" in token):
            out.append(token)
    return list(dict.fromkeys(out))


def musinsa_codes(product_or_entry):
    """Every code that may identify the product: the 품번 and codes in the name."""
    serial = product_or_entry.get("serial_number") or product_or_entry.get("serial") or ""
    name = product_or_entry.get("goodsName") or product_or_entry.get("name") or ""
    return list(dict.fromkeys([c for c in [serial.strip()] + name_codes(name) if c]))


def queries_for(product):
    """Model number first (most specific), codes in the name, then the product name."""
    out = []
    serial = (product.get("serial_number") or "").strip()
    name = (product.get("goodsName") or "").strip()
    if serial:
        out.append(serial)
    out.extend(name_codes(name))
    if name:
        out.append(name.split(" / ")[0].strip() if " / " in name else name)
    return list(dict.fromkeys(q for q in out if q))


def save_image(url, path):
    """Download a product thumbnail and store it as an RGB JPEG (max 512 px)."""
    if not url:
        return False
    if url.startswith("//"):
        url = "https:" + url
    host = requests.utils.urlparse(url).hostname or ""
    if not url.startswith("https://") or not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        return False
    response = requests.get(url, headers={"User-Agent": UA, "Accept": "image/*"}, timeout=20)
    if response.status_code != 200 or not response.content:
        return False
    image = Image.open(io.BytesIO(response.content))
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[-1])
        image = background
    else:
        image = image.convert("RGB")
    image.thumbnail((THUMB, THUMB))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    image.save(path, "JPEG", quality=88)
    return True


def collect(goods_nos=None, refresh=False, log=print):
    """Search KREAM for each product and save both sides' thumbnails."""
    if goods_nos is None:
        goods_nos = (store.get_setting("last_collection") or {}).get("goods") or []
    products = store.products(active_only=False, goods_nos=goods_nos)
    order = {str(g): i for i, g in enumerate(goods_nos)}
    products.sort(key=lambda p: order.get(str(p["goodsNo"]), 0))
    data = load()
    with L.bind(shop="kream", stage="match-review"), browser_transport("kream", start_url="https://kream.co.kr/products/74130"):
        kream_crawler.set_session_token(None)  # search is public; no token is sent
        for index, product in enumerate(products, 1):
            goods_no = str(product["goodsNo"])
            if goods_no in data and not refresh:
                continue
            entry = {"goodsNo": goods_no, "rank": product.get("rank"), "brand": product.get("brandName"),
                     "name": product.get("goodsName"), "serial": product.get("serial_number"),
                     "musinsaImage": None, "query": None, "candidates": [], "error": None,
                     "collectedAt": store.now()}
            folder = os.path.join(IMAGES, goods_no)
            try:
                if save_image(product.get("imageUrl"), os.path.join(folder, "musinsa.jpg")):
                    entry["musinsaImage"] = f"images/{goods_no}/musinsa.jpg"
                queries = queries_for(product)
                code_queries = set(musinsa_codes(product))
                for query in queries:
                    found = kream_crawler._extract_products_from_screen(kream_crawler._search_once(None, query) or {})
                    time.sleep(1.2)
                    if not found:
                        continue
                    entry["query"] = query if not entry["query"] else f"{entry['query']} | {query}"
                    known = {c["kreamId"] for c in entry["candidates"]}
                    for candidate in found[:MAX_CANDIDATES]:
                        kream_id = str(candidate.get("id") or "")
                        if not kream_id.isdigit() or kream_id in known:
                            continue
                        saved = save_image(candidate.get("image_url"), os.path.join(folder, f"kream_{kream_id}.jpg"))
                        entry["candidates"].append({
                            "kreamId": kream_id, "brand": candidate.get("brand"), "name": candidate.get("name_ko"),
                            "image": f"images/{goods_no}/kream_{kream_id}.jpg" if saved else None})
                    # keep going through the code queries (품번, codes in the name); stop before the free-text name
                    if query not in code_queries or len(entry["candidates"]) >= 2 * MAX_CANDIDATES:
                        break
            except CrawlError as exc:
                entry["error"] = str(exc)
                if exc.code in ("auth", "blocked", "rate_limit"):
                    data[goods_no] = entry
                    save(data)
                    raise
            except Exception as exc:
                entry["error"] = type(exc).__name__
            data[goods_no] = entry
            save(data)
            log(f"({index}/{len(products)}) {goods_no} 후보 {len(entry['candidates'])}개 (검색어: {entry['query']}) {entry['error'] or ''}")
    return data


MODEL_RE = re.compile(r"모델번호\s*([^<]{2,80})<")


def norm_code(code):
    return re.sub(r"[^A-Z0-9]", "", str(code or "").upper())


def usable_code(code):
    """A model code is evidence only if it is specific: 5+ characters with a digit."""
    code = norm_code(code)
    return len(code) >= 5 and any(ch.isdigit() for ch in code)


def code_match(musinsa_serial, kream_model):
    """'exact', 'near' or None.

    KREAM lists alternatives as "315122-001/CW2288-001".  'near' means one
    code contains the other after removing punctuation (a size/colour suffix
    such as "VN000D3HBKA1" vs "VN000D3HBKA"), with at least 6 shared characters.
    """
    mine = norm_code(musinsa_serial)
    if not usable_code(mine):
        return None
    best = None
    for part in re.split(r"[/,\s]+", str(kream_model or "")):
        theirs = norm_code(part)
        if not usable_code(theirs):
            continue
        if mine == theirs:
            return "exact"
        shorter, longer = sorted((mine, theirs), key=len)
        if len(shorter) >= 6 and longer.startswith(shorter) and len(longer) - len(shorter) <= 3:
            best = "near"
    return best


def fetch_models(goods_nos=None, log=print):
    """Read each candidate's 모델번호 from its KREAM product page (public HTML)."""
    data = load()
    if goods_nos is None:
        goods_nos = (store.get_setting("last_collection") or {}).get("goods") or list(data)
    seen = {c["kreamId"]: c["model"] for e in data.values() for c in e["candidates"] if c.get("model") is not None}
    with L.bind(shop="kream", stage="match-review"), browser_transport("kream", start_url="https://kream.co.kr/products/74130") as browser:
        for index, goods_no in enumerate(goods_nos, 1):
            entry = data.get(str(goods_no))
            if not entry:
                continue
            for candidate in entry["candidates"]:
                kream_id = candidate["kreamId"]
                if kream_id not in seen:
                    response = browser.get(f"https://kream.co.kr/products/{kream_id}", headers={"accept": "text/html"}, timeout=30)
                    if response.status_code in (401, 403, 429):
                        save(data)
                        raise CrawlError("KREAM", "blocked", f"상품 페이지 조회가 거절되었습니다 (HTTP {response.status_code}).")
                    found = MODEL_RE.search(response.text) if response.status_code == 200 else None
                    seen[kream_id] = found.group(1).strip() if found else ""
                    time.sleep(0.8)
                candidate["model"] = seen[kream_id]
            save(data)
            log(f"({index}/{len(goods_nos)}) {goods_no} {entry.get('serial')} -> " +
                ", ".join(f"{c['kreamId']}:{c.get('model') or '-'}" for c in entry["candidates"]))
    return data


def decide(goods_nos=None):
    """Model-code decision per product.

    'model_code'  exactly one candidate carries the Musinsa model code -> accept, no image step
    'ambiguous'   several candidates carry it (e.g. a collaboration edition shares the code) -> images decide
    'no_code'     no candidate carries it, or the Musinsa code is not specific -> images decide
    """
    data = load()
    if goods_nos is None:
        goods_nos = (store.get_setting("last_collection") or {}).get("goods") or list(data)
    out = []
    for goods_no in goods_nos:
        entry = data.get(str(goods_no))
        if not entry:
            continue
        hits = []
        for c in entry["candidates"]:
            kinds = [code_match(code, c.get("model")) for code in musinsa_codes(entry)]
            kind = "exact" if "exact" in kinds else "near" if "near" in kinds else None
            if kind:
                hits.append((c["kreamId"], kind, c.get("model")))
        exact = [h for h in hits if h[1] == "exact"] or hits
        if len(exact) == 1:
            decision, kream_id = "model_code", exact[0][0]
        elif len(exact) > 1:
            decision, kream_id = "ambiguous", ""
        else:
            decision, kream_id = "no_code", ""
        out.append({"goodsNo": str(goods_no), "serial": entry.get("serial"), "decision": decision, "kreamId": kream_id,
                    "kind": exact[0][1] if len(exact) == 1 else "", "hits": [f"{h[0]}({h[2]})" for h in exact]})
    return out


def apply_model_matches(goods_nos=None, log=print):
    """Register products whose model code identifies exactly one candidate.

    These skip the image step.  A KREAM product already matched (verified) to
    another Musinsa product is not reused.
    """
    from shop_comparator.storage import database
    from shop_comparator.services import pipeline
    registered = 0
    for row in decide(goods_nos):
        if row["decision"] != "model_code":
            continue
        if database.is_kream_id_already_used(row["kreamId"], row["goodsNo"], verified_only=True):
            log(f"{row['goodsNo']}: KREAM {row['kreamId']}은 이미 다른 상품에 연결되어 있어 건너뜁니다.")
            continue
        store.set_manual_match(row["goodsNo"], row["kreamId"], method="model_code", confidence=1.0 if row["kind"] == "exact" else 0.95,
                               note=f"모델번호 대조({row['kind']}): 무신사 {row['serial']} = KREAM {row['hits'][0]}")
        pipeline.blacklist_remove([row["goodsNo"]])
        registered += 1
    return registered


def fill_match_images(goods_nos=None, log=print):
    """Save thumbnails for products that were matched in the web app.

    The web job stores the matched KREAM product's image address but no image
    file, so the check sheet would show those rows without pictures.  This
    downloads the two thumbnails from the stored addresses (no search, no
    browser) and adds the matched product to the candidate list.
    """
    if goods_nos is None:
        goods_nos = (store.get_setting("last_collection") or {}).get("goods") or []
    wanted = {str(g) for g in goods_nos}
    urls = {r["musinsa_goods_no"]: r for r in store._rows(
        "SELECT musinsa_goods_no, kream_product_id, kream_product_name, kream_brand, kream_image_url FROM kream_search_cache")
        if r["musinsa_goods_no"] in wanted and r["kream_product_id"]}
    products = {str(p["goodsNo"]): p for p in store.products(active_only=False, goods_nos=list(wanted))}
    data, saved = load(), 0
    for goods_no, row in urls.items():
        product, kream_id = products.get(goods_no) or {}, str(row["kream_product_id"])
        entry = data.setdefault(goods_no, {
            "goodsNo": goods_no, "rank": product.get("rank"), "brand": product.get("brandName"), "name": product.get("goodsName"),
            "serial": product.get("serial_number"), "musinsaImage": None, "query": None, "candidates": [], "error": None,
            "collectedAt": store.now()})
        folder = os.path.join(IMAGES, goods_no)
        if not (entry.get("musinsaImage") and os.path.exists(os.path.join(ROOT, entry["musinsaImage"]))):
            try:
                if save_image(product.get("imageUrl"), os.path.join(folder, "musinsa.jpg")):
                    entry["musinsaImage"] = f"images/{goods_no}/musinsa.jpg"
                    saved += 1
            except Exception as exc:
                log(f"{goods_no} 무신사 이미지 저장 실패: {type(exc).__name__}")
        candidate = next((c for c in entry["candidates"] if str(c["kreamId"]) == kream_id), None)
        if candidate is None:
            candidate = {"kreamId": kream_id, "brand": row.get("kream_brand"), "name": row.get("kream_product_name"), "image": None, "model": ""}
            entry["candidates"].append(candidate)
        if not (candidate.get("image") and os.path.exists(os.path.join(ROOT, candidate["image"]))):
            try:
                if save_image(row.get("kream_image_url"), os.path.join(folder, f"kream_{kream_id}.jpg")):
                    candidate["image"] = f"images/{goods_no}/kream_{kream_id}.jpg"
                    saved += 1
            except Exception as exc:
                log(f"{goods_no} KREAM 이미지 저장 실패: {type(exc).__name__}")
    save(data)
    return saved


HOT_MONTHLY = 10      # KREAM sales in the last 30 days needed for a highlight
HOT_RATIO = 1.10      # KREAM instant-buy price / Musinsa member price for the strong highlight


def price_level(musinsa_price, kream_buy, monthly):
    """0 none, 1 KREAM is dearer and the product sells, 2 KREAM is at least 110% as well."""
    if not musinsa_price or not kream_buy or (monthly or 0) < HOT_MONTHLY or kream_buy <= musinsa_price:
        return 0
    return 2 if kream_buy * 100 >= musinsa_price * round(HOT_RATIO * 100) else 1   # integers: 100000 * 1.1 is not exact


def price_summary(size_rows, monthly):
    """Product-level numbers for the check sheet, from its comparable size rows."""
    pairs = [(r["musinsaPrice"], r["kreamBuy"], r) for r in size_rows if r.get("musinsaPrice") and r.get("kreamBuy")]
    musinsa = sorted({r["musinsaPrice"] for r in size_rows if r.get("musinsaPrice")})
    out = {"musinsa": "", "kream": "", "ratio": None, "difference": None, "size": "", "dearer": None, "compared": len(pairs), "level": 0}
    if musinsa:
        out["musinsa"] = musinsa[0] if len(musinsa) == 1 else f"{musinsa[0]:,}~{musinsa[-1]:,}"
    if not pairs:
        return out
    buys = sorted(k for _, k, _ in pairs)
    out["kream"] = buys[0] if buys[0] == buys[-1] else f"{buys[0]:,}~{buys[-1]:,}"
    price, buy, best = max(pairs, key=lambda item: item[1] / item[0])
    out.update(ratio=buy / price, difference=buy - price, size=best.get("size") or best.get("kreamSize") or "",
               dearer=sum(1 for m, k, _ in pairs if k > m), level=max(price_level(m, k, monthly) for m, k, _ in pairs))
    return out


def export_xlsx(goods_nos=None, path=None):
    """Excel for checking matches by eye.

    Sheet "매칭 확인": one row per Musinsa product with both ids, names, model
    codes, links, how the match was decided, and both thumbnails side by side.
    Sheet "사이즈별": every size row of the comparison for the same products.
    """
    import csv
    from datetime import datetime
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XlImage
    from openpyxl.styles import Alignment, Font, PatternFill
    from shop_comparator.services import comparison_service as cmp

    data = load()
    batch = (store.get_setting("last_collection") or {}).get("goods") or []
    matches = store.all_matches()
    if goods_nos is None:
        extra = [str(p["goodsNo"]) for p in store.products() if str(p["goodsNo"]) not in batch
                 and cmp.match_info(matches.get(str(p["goodsNo"])))["usable"]]
        goods_nos = list(batch) + extra
    products = {str(p["goodsNo"]): p for p in store.products(active_only=False, goods_nos=goods_nos)}
    verdicts = {}
    try:
        with open(os.path.join(ROOT, "verdicts.csv"), encoding="utf-8-sig") as f:
            verdicts = {r["musinsa_goods_no"]: r for r in csv.DictReader(f)}
    except OSError:
        pass
    label = {"model_code": "모델번호 일치", "image_review": "이미지 검토", "manual": "직접 지정"}
    try:
        with open(os.path.join(ROOT, "vlm_results.json"), encoding="utf-8") as f:
            vlm = json.load(f)
    except (OSError, ValueError):
        vlm = {}
    vlm_word = {"same": "동일", "different": "다름", "uncertain": "불확실", "error": "오류"}
    red, yellow, green = PatternFill("solid", fgColor="FBD5D0"), PatternFill("solid", fgColor="FFF2CC"), PatternFill("solid", fgColor="E2F0D9")
    wb = Workbook()
    ws = wb.active
    ws.title = "매칭 확인"
    from openpyxl.utils import get_column_letter
    PRICE_HEAD = ["무신사 회원가", "KREAM 즉시구매가", "KREAM÷무신사(최대)", "최대 차이(원)", "최대 차이 사이즈",
                  "KREAM이 더 비싼 사이즈 수", "비교한 사이즈 수", "최근 30일 체결"]
    head = ["번호", "매칭 상태", "무신사 이미지", "KREAM 이미지", "무신사 상품ID", "KREAM 상품ID", *PRICE_HEAD, "무신사 브랜드", "무신사 상품명",
            "KREAM 상품명", "무신사 품번", "KREAM 모델번호", "모델번호 대조", "매칭 방식", "신뢰도", "매칭 근거", "이미지 판정(참고)",
            "무신사 URL", "KREAM URL", "다른 KREAM 후보(번호:모델번호)", "직접 확인 결과(O/X)", "메모",
            "VLM 판정", "VLM이 고른 KREAM ID", "VLM 신뢰도", "VLM 사유", "대조 결과"]
    ws.append(head)
    for cell in ws[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = PatternFill("solid", fgColor="163A35")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    col = {name: get_column_letter(i) for i, name in enumerate(head, 1)}
    soft, strong = PatternFill("solid", fgColor="FFD966"), PatternFill("solid", fgColor="F4511E")
    order = sorted(goods_nos, key=lambda g: (not cmp.match_info(matches.get(str(g)))["usable"], goods_nos.index(g)))
    size_rows = {}
    for size_row in cmp.build([g for g in order if cmp.match_info(matches.get(str(g)))["usable"]], include_kream_only=False):
        size_rows.setdefault(size_row["goodsNo"], []).append(size_row)
    for index, goods_no in enumerate(order, 1):
        goods_no = str(goods_no)
        product, entry = products.get(goods_no, {}), data.get(goods_no, {})
        info = cmp.match_info(matches.get(goods_no))
        usable = info["usable"]
        kream_id = info["kreamId"] if usable else ""
        candidate = next((c for c in entry.get("candidates", []) if c["kreamId"] == kream_id), {})
        kream_name = candidate.get("name") or info.get("kreamName") or ""
        kinds = [code_match(code, candidate.get("model")) for code in musinsa_codes(product)] if kream_id else []
        kind = "exact" if "exact" in kinds else "near" if "near" in kinds else None
        others = ", ".join(f"{c['kreamId']}:{c.get('model') or '-'}" for c in entry.get("candidates", []) if c["kreamId"] != kream_id)
        verdict = verdicts.get(goods_no, {})
        v = vlm.get(goods_no) or {}
        v_id = str(v.get("kream_id") or "")
        cross, fill = "", None
        if v and v.get("at", "") < (entry.get("collectedAt") or ""):
            cross = "VLM 결과는 재검색 전 후보 기준(참고 불가)"   # judged before the candidate list changed
        elif v:
            if usable:
                if v.get("decision") == "same" and v_id == kream_id:
                    cross, fill = "일치 확인", green
                elif v.get("decision") == "same":
                    cross, fill = f"엇갈림: VLM은 다른 후보({v_id})를 선택", red
                elif v.get("decision") == "different":
                    cross, fill = "엇갈림: VLM은 다른 상품으로 판단", red
                elif v.get("decision") == "uncertain":
                    cross = "VLM 불확실"
            elif v.get("decision") == "same" and v_id:
                picked = next((c for c in entry.get("candidates", []) if c["kreamId"] == v_id), {})
                conflict = usable_code(picked.get("model")) and any(usable_code(c) for c in musinsa_codes(product)) \
                    and not any(code_match(c, picked.get("model")) for c in musinsa_codes(product))
                cross = f"VLM 제안: {v_id}" + (f" (단, 모델번호 {picked.get('model')}는 무신사 품번과 다름)" if conflict else "")
                fill = red if conflict else yellow
        state = info["status"] if usable else ("후보 있음·확정 못 함" if not info["kreamId"] else "예전 매칭(근거 없음, 사용 안 함)")
        if state == "자동 검증":
            state = "자동 판별기(미확인)"
        monthly = product.get("kream_monthly_transactions") if usable else None
        price = price_summary(size_rows.get(goods_no, []), monthly)
        ws.append([index, state, "", "",
                   goods_no, kream_id, price["musinsa"], price["kream"], price["ratio"], price["difference"], price["size"],
                   price["dearer"], price["compared"] if usable else None, monthly,
                   product.get("brandName") or "", product.get("goodsName") or "", kream_name,
                   product.get("serial_number") or "", candidate.get("model") or "",
                   {"exact": "일치", "near": "거의 일치"}.get(kind, "모델번호 없음/불일치" if kream_id else ""),
                   label.get(info["method"], info["method"]) if usable else "", info["confidence"] if usable else None,
                   info["reason"] if usable else "", f"{verdict.get('verdict', '')} {verdict.get('note', '')}".strip(),
                   f"https://www.musinsa.com/products/{goods_no}", f"https://kream.co.kr/products/{kream_id}" if kream_id else "", others, "", "",
                   vlm_word.get(v.get("decision"), ""), v_id, v.get("confidence") if v else None, v.get("reason", ""), cross])
        row = ws.max_row
        ws.row_dimensions[row].height = 92
        for cell in ws[row]:
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            if isinstance(cell.value, str):
                cell.data_type = "s"
        for column in (col["무신사 URL"], col["KREAM URL"]):
            cell = ws[f"{column}{row}"]
            if cell.value:
                cell.hyperlink = cell.value
                cell.font = Font(color="0563C1", underline="single")
        if not usable:
            for cell in ws[row]:
                cell.fill = PatternFill("solid", fgColor="F3F3F3")
        if fill:
            for name in ("매칭 상태", "VLM 판정", "VLM이 고른 KREAM ID", "VLM 신뢰도", "VLM 사유", "대조 결과"):
                ws[f"{col[name]}{row}"].fill = fill
        ws[f"{col['KREAM÷무신사(최대)']}{row}"].number_format = "0.0%"
        for name in ("무신사 회원가", "KREAM 즉시구매가", "최대 차이(원)"):
            ws[f"{col[name]}{row}"].number_format = "#,##0;[Red]-#,##0"
        if price["level"]:
            for name in ["무신사 상품ID", "KREAM 상품ID", *PRICE_HEAD]:
                cell = ws[f"{col[name]}{row}"]
                cell.fill = strong if price["level"] == 2 else soft
                cell.font = Font(bold=True, color="FFFFFF" if price["level"] == 2 else "000000")
        shown_kream = kream_id or (v_id if v.get("decision") == "same" else "")
        for column, rel in (("C", f"images/{goods_no}/musinsa.jpg"), ("D", f"images/{goods_no}/kream_{shown_kream}.jpg" if shown_kream else None)):
            full = os.path.join(ROOT, rel) if rel else None
            if full and os.path.exists(full):
                picture = Image.open(full)
                picture.thumbnail((116, 116))
                buffer = io.BytesIO()
                picture.save(buffer, "PNG")
                buffer.seek(0)
                ws.add_image(XlImage(buffer), f"{column}{row}")
    widths = [6, 14, 17, 17, 13, 13, 15, 18, 12, 12, 10, 11, 10, 10, 14, 38, 34, 18, 24, 14, 14,
              8, 40, 34, 30, 30, 40, 14, 24, 10, 14, 10, 50, 34]
    for index, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.freeze_panes = "G2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(head))}{ws.max_row}"

    sizes = wb.create_sheet("사이즈별")
    columns = [("goodsNo", "무신사 상품ID"), ("kreamId", "KREAM 상품ID"), ("goodsName", "무신사 상품명"), ("size", "무신사 사이즈"),
               ("kreamSize", "KREAM 사이즈"), ("sizeKey", "정규화 사이즈"), ("musinsaPrice", "무신사 회원가"), ("kreamBuy", "KREAM 즉시구매가"),
               ("kreamBuyDelivery", "배송 유형"), ("kreamSell", "KREAM 즉시판매가"), ("difference", "차이(KREAM-무신사)"),
               ("ratio", "KREAM÷무신사"), ("monthly", "최근 30일 체결"), ("status", "상태"), ("matchStatus", "상품 매칭")]
    sizes.append([title for _, title in columns])
    for cell in sizes[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = PatternFill("solid", fgColor="163A35")
    for row in cmp.build([g for g in order if cmp.match_info(matches.get(str(g)))["usable"]], include_kream_only=True):
        monthly = (products.get(row["goodsNo"]) or {}).get("kream_monthly_transactions")
        row = {**row, "monthly": monthly,
               "ratio": row["kreamBuy"] / row["musinsaPrice"] if row.get("kreamBuy") and row.get("musinsaPrice") else None}
        sizes.append([row.get(key) for key, _ in columns])
        level = price_level(row.get("musinsaPrice"), row.get("kreamBuy"), monthly)
        for cell in sizes[sizes.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = "s"
            if level:
                cell.fill = strong if level == 2 else soft
                cell.font = Font(bold=level == 2, color="FFFFFF" if level == 2 else "000000")
        sizes[f"L{sizes.max_row}"].number_format = "0.0%"
        for column in ("G", "H", "J", "K"):
            sizes[f"{column}{sizes.max_row}"].number_format = "#,##0;[Red]-#,##0"
    for index, width in enumerate([13, 13, 40, 12, 12, 12, 14, 16, 12, 16, 16, 13, 13, 22, 14], 1):
        sizes.column_dimensions[chr(64 + index)].width = width
    sizes.freeze_panes = "A2"
    sizes.auto_filter.ref = sizes.dimensions

    guide = wb.create_sheet("보는 법")
    for line in [
        ["매칭 상태", "모델번호 일치 = 무신사 품번과 KREAM 모델번호가 같은 후보가 하나뿐 / 이미지 검토 = 썸네일을 나란히 비교해 판정 / 직접 지정 = 사람이 KREAM 주소를 지정"],
        ["VLM 열", "gpt-5.4-mini가 저장된 이미지로 판정한 결과(참고용, 매칭 등록에는 반영하지 않음). 초록 = 등록된 매칭과 일치, 빨강 = 엇갈림 또는 모델번호 충돌, 노랑 = 미매칭 상품에 대한 VLM 제안(KREAM 이미지 칸에 제안 후보 표시)."],
        ["자동 판별기(미확인)", "프로그램의 기존 자동 매칭(상품명 규칙 + OpenAI 판별)이 등록한 것. 모델번호 대조를 거치지 않았으므로 직접 확인 권장."],
        ["후보 있음·확정 못 함", "KREAM 검색 결과는 있으나 같은 상품이라고 확정할 근거(모델번호 일치 등)가 없는 상품."],
        ["가격 열", "무신사 회원가와 KREAM 즉시구매가를 같은 사이즈끼리 비교한 값입니다. 'KREAM÷무신사(최대)'는 사이즈 중 가장 큰 비율이고, 사이즈별 값은 '사이즈별' 시트에 있습니다. KREAM 수수료·배송비는 반영하지 않았습니다."],
        ["가격 강조", f"노랑 = KREAM 즉시구매가가 무신사 회원가보다 높고 최근 30일 체결이 {HOT_MONTHLY}건 이상. 주황(흰 글씨) = 같은 조건에서 KREAM이 무신사의 {int(HOT_RATIO * 100)}% 이상. 체결 건수가 비어 있으면 강조하지 않습니다(거래량 필터를 실행해야 채워집니다)."],
        ["회색 행", "매칭되지 않은 상품. '다른 KREAM 후보' 열에 검색으로 나온 후보와 모델번호가 있습니다."],
        ["확인 방법", "이미지 두 칸과 품번·모델번호 두 칸을 비교하고, 필요하면 URL 두 개를 열어 보세요. 결과를 '직접 확인 결과' 열에 O/X로 적을 수 있습니다."],
        ["잘못된 매칭", "웹의 매칭 탭에서 해당 상품을 선택해 [선택 매칭 해제] 하거나 올바른 KREAM 주소로 [지정] 하세요."],
        ["생성 시각(UTC)", store.now()],
    ]:
        guide.append(line)
    guide.column_dimensions["A"].width = 18
    guide.column_dimensions["B"].width = 120

    if path is None:
        path = os.path.join(ROOT, f"match_check_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx")
    wb.save(path)
    return path


def export_opportunities(goods_nos=None, min_monthly=10, min_diff=10000, path=None):
    """Excel of sizes where KREAM is at least `min_diff` won above the Musinsa
    member price, for products with at least `min_monthly` KREAM sales in the
    last 30 days.

    Sheet "조건 충족 사이즈": one row per qualifying size.
    Sheet "상품 확인(이미지)": the products behind those rows with both
    thumbnails, ids, model codes and links, to re-check the match by eye.
    """
    from datetime import datetime
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XlImage
    from openpyxl.styles import Alignment, Font, PatternFill
    from shop_comparator.services import comparison_service as cmp

    data = load()
    if goods_nos is None:
        goods_nos = (store.get_setting("last_collection") or {}).get("goods") or []
    products = {str(p["goodsNo"]): p for p in store.products(active_only=False, goods_nos=goods_nos)}
    matches = store.all_matches()
    try:
        with open(os.path.join(ROOT, "vlm_results.json"), encoding="utf-8") as f:
            vlm = json.load(f)
    except (OSError, ValueError):
        vlm = {}
    rows = []
    for row in cmp.build(goods_nos, include_kream_only=False):
        product = products.get(row["goodsNo"], {})
        monthly = product.get("kream_monthly_transactions") or 0
        if row["difference"] is None or row["difference"] < min_diff or monthly < min_monthly:
            continue
        rows.append((row, product))
    rows.sort(key=lambda item: (-item[0]["difference"], item[0]["goodsNo"]))
    head_fill, head_font = PatternFill("solid", fgColor="163A35"), Font(color="FFFFFF", bold=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "조건 충족 사이즈"
    columns = ["무신사 랭킹", "무신사 상품ID", "KREAM 상품ID", "브랜드", "무신사 상품명", "무신사 사이즈", "KREAM 사이즈", "무신사 회원가",
               "KREAM 즉시구매가", "배송 유형", "차이(KREAM 즉시구매가-무신사)", "KREAM 즉시판매가", "차이(KREAM 즉시판매가-무신사)",
               "최근 30일 체결(최근 100건 중)", "누적 체결", "상품 매칭", "VLM 판정", "무신사 조회(UTC)", "KREAM 조회(UTC)", "무신사 URL", "KREAM URL"]
    ws.append(columns)
    for cell in ws[1]:
        cell.font, cell.fill = head_font, head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    word = {"same": "동일", "different": "다름", "uncertain": "불확실"}
    for row, product in rows:
        v = vlm.get(row["goodsNo"]) or {}
        v_text = word.get(v.get("decision"), "")
        if v.get("decision") == "same" and str(v.get("kream_id")) != row["kreamId"]:
            v_text = f"동일(다른 후보 {v.get('kream_id')})"
        ws.append([product.get("rank"), row["goodsNo"], row["kreamId"], row["brandName"], row["goodsName"], row["size"], row["kreamSize"],
                   row["musinsaPrice"], row["kreamBuy"], row["kreamBuyDelivery"], row["difference"], row["kreamSell"], row["sellDifference"],
                   product.get("kream_monthly_transactions"), product.get("kream_total_transactions"), row["matchStatus"], v_text,
                   row["musinsaCheckedAt"], row["kreamCheckedAt"], row["musinsaUrl"], row["kreamUrl"]])
        line = ws.max_row
        for cell in ws[line]:
            if isinstance(cell.value, str):
                cell.data_type = "s"
        for column in ("T", "U"):
            cell = ws[f"{column}{line}"]
            if cell.value:
                cell.hyperlink, cell.font = cell.value, Font(color="0563C1", underline="single")
        for column in ("H", "I", "K", "L", "M"):
            ws[f"{column}{line}"].number_format = "#,##0;[Red]-#,##0"
    for column, width in zip("ABCDEFGHIJKLMNOPQRSTU", [8, 13, 13, 14, 40, 11, 11, 13, 14, 11, 16, 14, 16, 14, 11, 14, 16, 20, 20, 30, 30]):
        ws.column_dimensions[column].width = width
    ws.freeze_panes = "F2"
    ws.auto_filter.ref = ws.dimensions

    check = wb.create_sheet("상품 확인(이미지)")
    head = ["무신사 랭킹", "무신사 이미지", "KREAM 이미지", "무신사 상품ID", "KREAM 상품ID", "브랜드", "무신사 상품명", "KREAM 상품명",
            "무신사 품번", "KREAM 모델번호", "상품 매칭", "매칭 근거", "VLM 판정", "VLM 신뢰도", "VLM 사유", "조건 충족 사이즈 수",
            "최대 차이", "조건 충족 사이즈", "최근 30일 체결", "무신사 URL", "KREAM URL", "직접 확인 결과(O/X)", "메모"]
    check.append(head)
    for cell in check[1]:
        cell.font, cell.fill = head_font, head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    grouped = {}
    for row, product in rows:
        grouped.setdefault(row["goodsNo"], []).append(row)
    for goods_no, items in sorted(grouped.items(), key=lambda kv: -max(r["difference"] for r in kv[1])):
        product, entry = products.get(goods_no, {}), data.get(goods_no, {})
        info = cmp.match_info(matches.get(goods_no))
        kream_id = info["kreamId"]
        candidate = next((c for c in entry.get("candidates", []) if c["kreamId"] == kream_id), {})
        v = vlm.get(goods_no) or {}
        v_text = word.get(v.get("decision"), "")
        mismatch = v.get("decision") == "different" or (v.get("decision") == "same" and str(v.get("kream_id")) != kream_id)
        if v.get("decision") == "same" and str(v.get("kream_id")) != kream_id:
            v_text = f"동일(다른 후보 {v.get('kream_id')})"
        check.append([product.get("rank"), "", "", goods_no, kream_id, product.get("brandName") or "", product.get("goodsName") or "",
                      candidate.get("name") or info.get("kreamName") or "", product.get("serial_number") or "", candidate.get("model") or "",
                      info["status"], info["reason"], v_text, v.get("confidence") if v else None, v.get("reason", ""), len(items),
                      max(r["difference"] for r in items), ", ".join(f"{r['size']}(+{r['difference']:,})" for r in items),
                      product.get("kream_monthly_transactions"), items[0]["musinsaUrl"], items[0]["kreamUrl"], "", ""])
        line = check.max_row
        check.row_dimensions[line].height = 92
        for cell in check[line]:
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            if isinstance(cell.value, str):
                cell.data_type = "s"
        for column in ("T", "U"):
            cell = check[f"{column}{line}"]
            if cell.value:
                cell.hyperlink, cell.font = cell.value, Font(color="0563C1", underline="single")
        if mismatch:
            for column in ("K", "M", "N", "O"):
                check[f"{column}{line}"].fill = PatternFill("solid", fgColor="FBD5D0")
        for column, rel in (("B", f"images/{goods_no}/musinsa.jpg"), ("C", f"images/{goods_no}/kream_{kream_id}.jpg")):
            full = os.path.join(ROOT, rel)
            if os.path.exists(full):
                picture = Image.open(full)
                picture.thumbnail((116, 116))
                buffer = io.BytesIO()
                picture.save(buffer, "PNG")
                buffer.seek(0)
                check.add_image(XlImage(buffer), f"{column}{line}")
    for column, width in zip("ABCDEFGHIJKLMNOPQRSTUVW", [8, 17, 17, 13, 13, 14, 38, 32, 18, 22, 14, 38, 16, 9, 44, 9, 12, 44, 10, 30, 30, 14, 22]):
        check.column_dimensions[column].width = width
    check.freeze_panes = "D2"

    note = wb.create_sheet("기준")
    for line in [
        ["대상", f"무신사 신발 랭킹 수집분 {len(goods_nos)}개 중 KREAM과 매칭되어 가격을 비교한 상품"],
        ["조건 1", f"KREAM 최근 30일 체결 {min_monthly}건 이상 (상품 단위). 체결 내역은 최근 100건까지만 조회하므로 100이 상한"],
        ["조건 2", f"사이즈별로 KREAM 즉시구매 최저가 − 무신사 회원가 ≥ {min_diff:,}원"],
        ["무신사 회원가", cmp.PRICE_NOTE],
        ["KREAM 즉시구매가", "새 상품을 바로 살 수 있는 가격 중 최저(배송 유형 표시). 지금 무신사에서 사서 KREAM에 바로 팔 때 받는 값은 '즉시판매가' 열이며 수수료·배송비는 빠져 있지 않음"],
        ["빨간 칸", "'상품 확인' 시트에서 VLM이 등록된 매칭과 다르게 판단한 상품. 우선 확인 권장"],
        ["결과", f"조건 충족 사이즈 {len(rows)}행, 상품 {len(grouped)}개"],
        ["생성 시각(UTC)", store.now()],
    ]:
        note.append(line)
    note.column_dimensions["A"].width = 18
    note.column_dimensions["B"].width = 120
    if path is None:
        path = os.path.join(ROOT, f"opportunities_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx")
    wb.save(path)
    return path, len(rows), len(grouped)


def _font(size):
    for name in ("malgun.ttf", "malgunbd.ttf", "gulim.ttc"):
        try:
            return ImageFont.truetype(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name), size)
        except OSError:
            continue
    return ImageFont.load_default()


def sheets(goods_nos=None, per_sheet=8, cell=210):
    """Contact sheets: one row per Musinsa product, its image then each KREAM candidate."""
    data = load()
    if goods_nos is None:
        goods_nos = (store.get_setting("last_collection") or {}).get("goods") or list(data)
    entries = [data[str(g)] for g in goods_nos if str(g) in data]
    os.makedirs(SHEETS, exist_ok=True)
    font, small = _font(15), _font(12)
    label_h, columns = 40, 1 + MAX_CANDIDATES
    paths = []
    for start in range(0, len(entries), per_sheet):
        chunk = entries[start:start + per_sheet]
        sheet = Image.new("RGB", (columns * cell, len(chunk) * (cell + label_h)), (255, 255, 255))
        draw = ImageDraw.Draw(sheet)
        for row, entry in enumerate(chunk):
            top = row * (cell + label_h)
            cells = [(entry.get("musinsaImage"), f"#{start + row + 1} 무신사 {entry['goodsNo']}", (entry.get("serial") or "")[:24])]
            for candidate in entry["candidates"]:
                cells.append((candidate.get("image"), f"KREAM {candidate['kreamId']}", (candidate.get("name") or "")[:16]))
            for column, (image_path, title, sub) in enumerate(cells):
                left = column * cell
                if image_path and os.path.exists(os.path.join(ROOT, image_path)):
                    picture = Image.open(os.path.join(ROOT, image_path))
                    picture.thumbnail((cell - 8, cell - 8))
                    sheet.paste(picture, (left + (cell - picture.width) // 2, top + label_h + (cell - picture.height) // 2))
                else:
                    draw.text((left + 60, top + label_h + cell // 2), "이미지 없음", fill=(150, 150, 150), font=small)
                draw.rectangle([left, top, left + cell - 1, top + label_h - 1], fill=(22, 58, 53) if column == 0 else (238, 242, 241))
                draw.text((left + 6, top + 3), title, fill=(255, 255, 255) if column == 0 else (20, 33, 31), font=font)
                draw.text((left + 6, top + 22), sub, fill=(220, 230, 228) if column == 0 else (93, 107, 104), font=small)
            draw.line([0, top, columns * cell, top], fill=(200, 200, 200))
        path = os.path.join(SHEETS, f"sheet_{start // per_sheet + 1:02d}.jpg")
        sheet.save(path, "JPEG", quality=85)
        paths.append(path)
    return paths


if __name__ == "__main__":
    store.init()
    command = sys.argv[1] if len(sys.argv) > 1 else "collect"
    ids = sys.argv[2:] or None
    if command == "collect":
        result = collect(ids)
        print(f"저장: {CANDIDATES} ({len(result)}개 상품)")
    elif command == "models":
        fetch_models(ids)
        for row in decide(ids):
            print(row)
    elif command == "opportunities":
        print(export_opportunities())
    elif command == "export":
        if ids == ["last"]:  # only the batch collected last, not every stored match
            ids = (store.get_setting("last_collection") or {}).get("goods") or []
        print("이미지 저장:", fill_match_images(ids, log=print))
        print(export_xlsx(ids))
    elif command == "apply":
        print("모델번호 일치로 등록:", apply_model_matches(ids))
    elif command == "decide":
        for row in decide(ids):
            print(row)
    elif command == "sheets":
        for created in sheets(ids):
            print(created)

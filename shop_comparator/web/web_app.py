"""Local, single-user web control panel. Run: python main.py"""
import csv
import io
import os
import re
import secrets
import zipfile
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_file

from shop_comparator.core import app_logging as L
from shop_comparator.services import comparison_service as cmp
from shop_comparator.storage import database
from shop_comparator.services import exports
from shop_comparator.services import monitoring
from shop_comparator.services import pipeline
from shop_comparator.services import vlm_review
from shop_comparator.matching import gold, gold_eval
from shop_comparator.shops import site_login
from shop_comparator.storage import store
from shop_comparator.core import webhook
from shop_comparator.shops.browser_transport import is_busy
from shop_comparator.services.comparison_service import product_id
from shop_comparator.core.crawl_errors import CrawlError
from shop_comparator.services.jobs import JobManager
from shop_comparator.core.utils import get_musinsa_brands_from_csv, get_persistent_path

SITES = ("musinsa", "kream", "poizon")
SITE_LABEL = {"musinsa": "무신사", "kream": "KREAM", "poizon": "POIZON"}
DEADLINES = ["무기한", "60일", "30일", "7일"]
MAX_LIMIT = 1000
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _int(value, name, low, high):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name}은(는) {low}~{high} 범위의 정수여야 합니다.")
    if not low <= number <= high:
        raise ValueError(f"{name}은(는) {low}~{high} 범위여야 합니다.")
    return number


def _ids(values, service="musinsa", limit=MAX_LIMIT):
    if isinstance(values, str):
        values = values.split()
    if not isinstance(values, list):
        raise ValueError("상품 목록 형식이 올바르지 않습니다.")
    out = list(dict.fromkeys(product_id(v, service) for v in values if str(v).strip()))
    if len(out) > limit:
        raise ValueError(f"한 번에 최대 {limit}개 상품까지 처리할 수 있습니다.")
    return out


def validate_settings(body):
    """Validate a Musinsa collection request (also used by full runs)."""
    mode = body.get("mode", "products")
    if mode not in {"products", "ranking", "category", "brand", "current", "selected"}:
        raise ValueError("잘못된 수집 방식입니다.")
    price_mode = body.get("price_mode", "grouped")
    if price_mode not in pipeline.PRICE_MODES:
        raise ValueError("잘못된 회원가 조회 방식입니다.")
    settings = {"mode": mode, "price_mode": price_mode, "replace_list": bool(body.get("replace_list")),
                "ids": [], "kream_ids": {}, "rankings": [], "categories": [], "brands": [],
                "limit": _int(body.get("limit", 10), "수집 개수", 1, MAX_LIMIT)}
    if mode == "products":
        for line in str(body.get("products", "")).splitlines():
            if not line.strip():
                continue
            columns = line.split()
            if len(columns) > 2:
                raise ValueError("한 줄에 무신사 URL과 선택적인 KREAM URL을 입력해 주세요.")
            gid = product_id(columns[0], "musinsa")
            if gid not in settings["ids"]:
                settings["ids"].append(gid)
            if len(columns) == 2:
                settings["kream_ids"][gid] = product_id(columns[1], "kream")
        if not settings["ids"]:
            raise ValueError("무신사 상품을 하나 이상 입력해 주세요.")
        if len(settings["ids"]) > MAX_LIMIT:
            raise ValueError(f"한 번에 최대 {MAX_LIMIT}개 상품을 입력해 주세요.")
    elif mode == "selected":
        settings["ids"] = _ids(body.get("ids") or [])
        if not settings["ids"]:
            raise ValueError("갱신할 상품을 선택해 주세요.")
    elif mode == "ranking":
        settings["rankings"] = [c for c in body.get("rankings") or [] if c in pipeline.RANKINGS]
        if not settings["rankings"]:
            raise ValueError("랭킹을 하나 이상 선택해 주세요.")
    elif mode == "category":
        categories = body.get("categories") or ([body["category"]] if body.get("category") else [])
        if any(str(c) not in pipeline.CATEGORIES for c in categories):
            raise ValueError("지원하지 않는 카테고리입니다.")
        settings["categories"] = [str(c) for c in categories]
        if not settings["categories"]:
            raise ValueError("카테고리를 하나 이상 선택해 주세요.")
    elif mode == "brand":
        known = set(brand_list())
        settings["brands"] = [str(b) for b in body.get("brands") or [] if str(b) in known]
        if not settings["brands"]:
            raise ValueError("브랜드를 하나 이상 선택해 주세요.")
    return settings


def find_csv(prefix):
    """First CSV starting with prefix; original exports before generated ones (GUI rule)."""
    try:
        names = sorted((n for n in os.listdir(get_persistent_path(".")) if n.startswith(prefix) and n.endswith(".csv")),
                       key=lambda n: ("generated" in n.lower(), n.lower()))
    except OSError:
        names = []
    return get_persistent_path(names[0]) if names else None


def brand_list():
    path = find_csv("kream_overlapping_brands")
    return [str(b) for b in get_musinsa_brands_from_csv(path)] if path else []


ENV_KEYS = {"musinsa": ("MUSINSA_ID", "MUSINSA_PASSWORD"), "kream": ("KREAM_ID", "KREAM_PASSWORD"),
            "poizon": ("POIZON_ID", "POIZON_PASSWORD")}


def env_credentials(site, reload=True):
    """Account from .env (MUSINSA_ID/MUSINSA_PASSWORD, KREAM_ID/KREAM_PASSWORD).

    Values are read at the moment of login and are never returned by any API.
    """
    if reload:
        try:
            from shop_comparator.matching.product_matcher import load_local_env
            load_local_env()
        except Exception:
            pass
    account = (os.getenv(ENV_KEYS[site][0]) or "").strip().strip('"').strip("'")
    password = (os.getenv(ENV_KEYS[site][1]) or "").strip().strip('"').strip("'")
    return (account, password) if account and password else None


def mask_webhook(url):
    return f"…{url[-6:]}" if url else ""


def create_app(manager=None, monitors=None, auto=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
    jobs = manager or JobManager()
    monitors = monitors or {kind: monitoring.Monitor(kind) for kind in monitoring.KINDS}
    auto = auto or monitoring.AutoEvent()
    csrf = secrets.token_urlsafe(32)
    app.jobs, app.monitors, app.auto = jobs, monitors, auto

    @app.before_request
    def local_only():
        # Bind + Host check prevent DNS rebinding; CSRF protects local login/crawl actions.
        if request.remote_addr not in {"127.0.0.1", "::1"} or urlparse(request.host_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            return jsonify(error="이 PC에서만 접속할 수 있습니다."), 403
        if request.method == "POST" and not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), csrf):
            return jsonify(error="페이지를 새로고침한 뒤 다시 시도해 주세요."), 403

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                                                       "img-src 'self' https://image.msscdn.net https://*.msscdn.net; frame-ancestors 'none'")
        return response

    @app.errorhandler(ValueError)
    def bad_request(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(CrawlError)
    def crawl_error(exc):
        return jsonify(error=str(exc), kind=exc.code), 409 if exc.code in ("busy", "prerequisite") else 502

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("JSON 입력이 필요합니다.")
        return data

    def start(kind, title, resources, action):
        job_id, conflict = jobs.start(kind, title, resources, action)
        if not job_id:
            return jsonify(error=f"'{conflict}' 작업이 같은 쇼핑몰을 사용 중입니다. 끝나거나 중단한 뒤 다시 실행해 주세요."), 409
        return jsonify(ok=True, job=job_id), 202

    def last_collection():
        saved = store.get_setting("last_collection") or {}
        active = {str(p["goodsNo"]) for p in store.products()}
        return {"goods": [g for g in saved.get("goods") or [] if g in active], "at": saved.get("at")}

    def selection(data):
        """Which products a follow-up job works on.

        Explicit ids (checked products) win.  Otherwise the default is the
        products of the most recent Musinsa collection, not the whole stored
        list; the whole list only when the user chooses scope "all".
        """
        if data.get("ids"):
            return _ids(data["ids"])
        if data.get("scope") == "all":
            return None
        goods = last_collection()["goods"]
        if not goods:
            raise CrawlError("대상", "prerequisite", "방금 수집한 무신사 상품이 없습니다. 먼저 [무신사만 수집]을 실행하거나, 대상을 '상품 목록 전체'로 바꾸거나, 상품·비교 탭에서 상품을 선택해 주세요.")
        return goods

    # ------------------------------------------------------------ pages
    @app.get("/")
    def index():
        return render_template("index.html", csrf=csrf, categories=pipeline.CATEGORIES,
                               rankings={k: v[0] for k, v in pipeline.RANKINGS.items()},
                               deadlines=DEADLINES, monitors=monitoring.KINDS, price_note=cmp.PRICE_NOTE,
                               demo=os.getenv("SHOP_DEMO_LABEL") or "")

    @app.get("/favicon.ico")
    def favicon():
        return "", 204

    @app.get("/api/state")
    def state():
        return jsonify(
            sessions=site_login.status(), jobs=jobs.snapshot(), busy=jobs.busy_resources(),
            browsers={s: is_busy(s) for s in ("musinsa", "kream")}, counts=store.counts(),
            envAccounts={s: bool(env_credentials(s, reload=False)) for s in SITES},
            lastCollection={"count": len(last_collection()["goods"]), "at": last_collection()["at"]},
            monitors={k: {"running": m.running(), "items": store.monitor_count(k)} for k, m in monitors.items()},
            auto=auto.snapshot(), logSeq=L.last_seq())

    # ------------------------------------------------------------ login
    @app.post("/api/login/<site>")
    def login(site):
        if site not in SITES:
            raise ValueError("지원하지 않는 쇼핑몰입니다.")
        data = body()
        manual = bool(data.get("manual"))
        account, password = str(data.get("account") or "").strip(), str(data.get("password") or "")
        remember = bool(data.get("remember"))
        if data.get("env"):
            found = env_credentials(site)
            if not found:
                raise ValueError(f".env에 {ENV_KEYS[site][0]} / {ENV_KEYS[site][1]} 값이 없습니다.")
            account, password = found
            L.register_secret(password)
        if not manual and (not account or not password):
            raise ValueError(f"{SITE_LABEL[site]} 아이디와 비밀번호를 입력해 주세요.")
        if len(account) > 200 or len(password) > 200:
            raise ValueError("입력값이 너무 깁니다.")
        if site == "poizon":
            if manual:
                raise ValueError("POIZON은 아이디/비밀번호 로그인만 지원합니다.")
            def action(ctx):
                pipeline.login_poizon(account, password, ctx)
                if remember:
                    site_login.vault.put("poizon", account, password)
            return start("login", "POIZON 로그인", ["poizon"], action)
        fn = site_login.login_musinsa if site == "musinsa" else site_login.login_kream
        creds = ("", "") if manual else (account, password)
        return start("login", f"{SITE_LABEL[site]} 로그인", [site],
                     lambda ctx: fn(creds[0], creds[1], ctx.cancel, remember=remember))

    @app.post("/api/session/<site>/check")
    def session_check(site):
        if site not in ("musinsa", "kream"):
            raise ValueError("지원하지 않는 쇼핑몰입니다.")
        return start("session", f"{SITE_LABEL[site]} 세션 확인", [site], lambda ctx: site_login.check_session(site))

    @app.post("/api/session/<site>/forget")
    def session_forget(site):
        if site not in SITES:
            raise ValueError("지원하지 않는 쇼핑몰입니다.")
        site_login.vault.drop(site)
        return jsonify(ok=True)

    # ------------------------------------------------------------- jobs
    @app.post("/api/jobs/<kind>")
    def job(kind):
        data = body()
        if kind == "musinsa":
            settings = validate_settings(data)
            return start(kind, "무신사 수집", ["musinsa"], lambda ctx: pipeline.collect_musinsa(settings, ctx))
        if kind == "match":
            ids, force = selection(data), bool(data.get("force"))
            return start(kind, "KREAM 매칭", ["kream"], lambda ctx: pipeline.match_kream(ids, ctx, force))
        if kind == "kream":
            ids = selection(data)
            # Same as the GUI's "크림 가격 추출": products without a match are searched first.
            return start(kind, "KREAM 가격 수집", ["kream"], lambda ctx: pipeline.collect_kream(ids, ctx, match_missing=True))
        if kind == "compare":
            ids = selection(data)
            return start(kind, "가격 비교", [], lambda ctx: pipeline.compare(ids, ctx))
        if kind == "full":
            settings = validate_settings(data)
            return start(kind, "전체 순차 실행", ["musinsa", "kream"], lambda ctx: pipeline.full(settings, ctx))
        if kind == "filter":
            ids = selection(data)
            low = _int(data.get("min_total", 0) or 0, "최소 거래건수", 0, 10**9)
            monthly = _int(data.get("min_monthly", 0) or 0, "최소 한달 거래수", 0, 10**9)
            return start(kind, "KREAM 거래량 필터", ["kream"], lambda ctx: pipeline.kream_filter(ids, low, monthly, ctx))
        if kind == "vlm":
            ids, mode, force = selection(data), data.get("mode", "stored"), bool(data.get("force"))
            if mode not in ("stored", "live"):
                raise ValueError("실행 방식이 올바르지 않습니다.")
            max_calls = None
            if mode == "live":       # paid: never without an explicit, bounded call count
                max_calls = _int(data.get("max_calls"), "최대 호출 수", 1, 500)
            title = "VLM 검토 (저장된 판정 재생)" if mode == "stored" else f"VLM 검토 (실시간, 최대 {max_calls}회)"
            return start(kind, title, ["vlm"], lambda ctx: vlm_review.review_products(ids, ctx, mode, max_calls, force))
        if kind in ("poizon", "full_poizon"):
            creds = site_login.vault.get("poizon") or (str(data.get("account") or "").strip(), str(data.get("password") or ""))
            if not creds[0] or not creds[1]:
                raise ValueError("POIZON 아이디와 비밀번호를 로그인 탭에 입력해 주세요.")
            if kind == "poizon":
                ids = selection(data)
                return start(kind, "POIZON 가격 수집", ["poizon"], lambda ctx: pipeline.collect_poizon(ids, creds[0], creds[1], ctx))
            settings = validate_settings(data)
            return start(kind, "무신사+POIZON 순차 실행", ["musinsa", "poizon"], lambda ctx: pipeline.full(settings, ctx, with_poizon=creds))
        raise ValueError("지원하지 않는 작업입니다.")

    @app.post("/api/stop")
    def stop():
        data = request.get_json(silent=True) or {}
        return jsonify(ok=True, stopped=jobs.stop(data.get("id")))

    @app.get("/api/jobs/history")
    def history():
        rows = store.recent_jobs()
        for row in rows:
            row["items"] = store.job_item_summary(row["id"])
            row["unfinished"] = sum(len(v) for v in pipeline.unfinished(row["id"]).values())
        return jsonify(jobs=rows)

    @app.get("/api/jobs/<job_id>/items")
    def job_items(job_id):
        if not store.job_run(job_id):
            return jsonify(error="작업을 찾을 수 없습니다."), 404
        return jsonify(summary=store.job_item_summary(job_id), unfinished=pipeline.unfinished(job_id),
                       items=[i for i in store.job_items(job_id) if i["state"] != "ok"][:500])

    @app.post("/api/jobs/<job_id>/resume")
    def job_resume(job_id):
        """Run only what an earlier job left failed or untouched.  Finished products are not requested again."""
        previous = store.job_run(job_id)
        if not previous:
            return jsonify(error="작업을 찾을 수 없습니다."), 404
        if previous["state"] in ("running", "stopping"):
            raise CrawlError("작업", "busy", "아직 실행 중인 작업입니다. 끝나거나 중단한 뒤 다시 실행해 주세요.")
        todo = pipeline.unfinished(job_id)
        if not todo:
            raise CrawlError("작업", "prerequisite", "다시 실행할 실패·미처리 항목이 없습니다.")
        price_mode = previous["summary"].get("priceMode") or "grouped"
        if price_mode not in pipeline.PRICE_MODES:
            price_mode = "grouped"

        def action(ctx):
            ctx.note("resumeOf", job_id)
            if todo.get("musinsa"):
                pipeline.resume_musinsa(todo["musinsa"], price_mode, ctx)
            if todo.get("kream"):
                pipeline.collect_kream(todo["kream"], ctx)
            if todo.get("vlm"):
                vlm_review.review_products(todo["vlm"], ctx, "stored")     # a paid run is never repeated automatically

        resources = [r for r in ("musinsa", "kream", "vlm") if todo.get(r)]
        total = sum(len(v) for v in todo.values())
        return start("resume", f"실패·미처리 {total}개 재실행 ({previous['title']})", resources, action)

    # --------------------------------------------------------- products
    @app.get("/api/products")
    def products():
        show_hidden = request.args.get("hidden") == "1"
        items = store.products(active_only=not show_hidden)
        total = len(items)
        batch = last_collection()
        if request.args.get("scope", "last") == "last" and batch["goods"]:
            # Default view: exactly what was just collected, in collection order.
            order = {g: i for i, g in enumerate(batch["goods"])}
            items = sorted((p for p in items if str(p["goodsNo"]) in order), key=lambda p: order[str(p["goodsNo"])])
        matches = store.all_matches()
        rows = cmp.build([p["goodsNo"] for p in items]) if items else []
        by_goods = {}
        for row in rows:
            by_goods.setdefault(row["goodsNo"], []).append(row)
        out = []
        for p in items:
            goods_no = str(p["goodsNo"])
            info = cmp.match_info(matches.get(goods_no))
            out.append({
                "goodsNo": goods_no, "rank": p.get("rank"), "goodsName": p.get("goodsName") or "", "brandName": p.get("brandName") or "",
                "serial": p.get("serial_number") or "", "imageUrl": p.get("imageUrl") or "", "active": bool(p["active"]),
                "finalPrice": p.get("finalPrice"), "soldout": bool(p.get("soldout")),
                "musinsaStatus": p.get("musinsaStatus"), "musinsaError": p.get("musinsaError"), "musinsaCheckedAt": p.get("musinsaCheckedAt"),
                "kreamStatus": p.get("kreamStatus"), "kreamError": p.get("kreamError"), "kreamCheckedAt": p.get("kreamCheckedAt"),
                "poizonStatus": p.get("poizonStatus"), "poizonLink": p.get("poizonLink") or "",
                "match": info, "totalTrades": p.get("kream_total_transactions"), "monthlyTrades": p.get("kream_monthly_transactions"),
                "sizes": by_goods.get(goods_no, []),
            })
        return jsonify(products=out, summary=cmp.summarize(rows), note=cmp.PRICE_NOTE, total=total,
                       lastCollection={"count": len(batch["goods"]), "at": batch["at"]})

    @app.get("/api/products/<goods_no>")
    def product_detail(goods_no):
        goods_no = product_id(goods_no, "musinsa")
        p = store.product(goods_no)
        if not p:
            return jsonify(error="상품을 찾을 수 없습니다."), 404
        keep = ["goodsNo", "rank", "goodsName", "brandName", "serial_number", "linkUrl", "imageUrl", "baseCategoryFullName",
                "normalPrice", "saleRate", "salePrice", "couponDiscount", "finalPrice", "memberDiscountRate", "memberSavePointRate",
                "isMemberDiscount", "isUsePointDiscount", "goodsSavePoint", "availableSizes", "managedCode", "couponName",
                "kream_total_transactions", "kream_monthly_transactions", "lastObservedFinalPrice", "musinsaCheckedAt", "kreamCheckedAt"]
        return jsonify(product={k: p.get(k) for k in keep}, match=cmp.match_info(store.match_for(goods_no)),
                       sizes=cmp.build([goods_no]))

    @app.post("/api/products/active")
    def products_active():
        data = body()
        ids = _ids(data.get("ids") or [])
        if not ids:
            raise ValueError("상품을 선택해 주세요.")
        store.set_active(ids, bool(data.get("active")))
        L.log(f"상품 {len(ids)}개를 목록에서 {'복원' if data.get('active') else '제외(숨김)'}했습니다.", stage="list")
        return jsonify(ok=True)

    # ---------------------------------------------------------- matches
    @app.get("/api/matches")
    def matches():
        cache = store.all_matches()
        black = set(pipeline.blacklist())
        out = []
        for p in store.products():
            goods_no = str(p["goodsNo"])
            out.append({"goodsNo": goods_no, "goodsName": p.get("goodsName") or "", "brandName": p.get("brandName") or "",
                        "serial": p.get("serial_number") or "", "blacklisted": goods_no in black,
                        **cmp.match_info(cache.get(goods_no))})
        return jsonify(matches=out, trustUnverified=cmp.trust_unverified())

    @app.post("/api/matches")
    def set_match():
        data = body()
        goods_no = product_id(data.get("goodsNo", ""), "musinsa")
        kream_id = product_id(data.get("kream", ""), "kream")
        if not store.product(goods_no):
            raise ValueError("목록에 없는 무신사 상품입니다.")
        store.set_manual_match(goods_no, kream_id)
        pipeline.blacklist_remove([goods_no])
        L.log(f"KREAM 상품 {kream_id}을(를) 직접 지정했습니다.", goods=goods_no, shop="kream", stage="kream-match")
        return jsonify(ok=True)

    @app.post("/api/matches/<action>")
    def match_action(action):
        ids = _ids(body().get("ids") or [])
        if not ids:
            raise ValueError("상품을 선택해 주세요.")
        if action == "approve":
            # the same decision as in the review tab: recorded in the review history, source of the match kept
            for goods_no in ids:
                current = cmp.match_info(store.match_for(goods_no))
                if not current["kreamId"]:
                    raise ValueError(f"상품 {goods_no}에는 승인할 KREAM 매칭이 없습니다.")
                vlm_review.decide(goods_no, "approve", current["kreamId"], "매칭 탭에서 승인")
        elif action == "clear":
            for goods_no in ids:
                store.clear_match(goods_no)
        elif action == "unblacklist":
            pipeline.blacklist_remove(ids)
        else:
            raise ValueError("지원하지 않는 동작입니다.")
        L.log(f"매칭 {action}: 상품 {len(ids)}개", shop="kream", stage="kream-match")
        return jsonify(ok=True)

    # ----------------------------------------------------------- review
    @app.get("/api/review")
    def review_list():
        ids = _ids(request.args.get("ids", "").split(",")) if request.args.get("ids") else \
            (list(last_collection()["goods"]) if request.args.get("scope", "last") == "last" else [str(p["goodsNo"]) for p in store.products()])
        items = vlm_review.queue(ids)
        return jsonify(items=items, metrics=vlm_review.metrics(ids), confidenceNote=vlm_review.CONFIDENCE_NOTE,
                       liveAvailable=bool(os.getenv("OPENAI_API_KEY_VLM")), storedVerdicts=len(vlm_review.stored_verdicts()))

    @app.get("/api/review/image/<goods_no>/<name>")
    def review_image(goods_no, name):
        from shop_comparator.matching import match_review
        if not goods_no.isdigit() or not re.fullmatch(r"(musinsa|kream_\d{1,12})\.jpg", name):
            return jsonify(error="이미지를 찾을 수 없습니다."), 404
        path = os.path.join(match_review.IMAGES, goods_no, name)
        if not os.path.isfile(path):
            return jsonify(error="이미지를 찾을 수 없습니다."), 404
        return send_file(os.path.abspath(path), mimetype="image/jpeg")

    @app.post("/api/review/<goods_no>")
    def review_decide(goods_no):
        data = body()
        goods_no = product_id(goods_no, "musinsa")
        review_id = vlm_review.decide(goods_no, data.get("action"), data.get("kreamId"), data.get("note"),
                                      data.get("seconds"), data.get("mode", "assisted"))
        # start/end and time away from the tab go to the evaluation store, not to the working database
        gold.add_timing("ops", goods_no, data.get("mode", "assisted"), data.get("action"), data.get("kreamId"), data.get("reviewer"),
                        data.get("startedAt"), data.get("endedAt"), data.get("seconds"), data.get("hiddenSeconds"),
                        data.get("interruptions"), review_id)
        return jsonify(ok=True, review=review_id)

    # ------------------------------------------------- ground-truth labelling
    @app.get("/api/gold")
    def gold_list():
        reviewer = " ".join(request.args.get("reviewer", "").split())[:40]
        return jsonify(items=gold.queue(reviewer or None), status=gold.status(), automation=gold.origin_now() == "automation")

    @app.get("/api/gold/image/<case_id>/<name>")
    def gold_image(case_id, name):
        if not re.fullmatch(r"G\d{3}", case_id) or not re.fullmatch(r"(musinsa|kream_\d{1,12})\.jpg", name):
            return jsonify(error="이미지를 찾을 수 없습니다."), 404
        file_path = gold.path("images", case_id, name)
        if not os.path.isfile(file_path):
            return jsonify(error="이미지를 찾을 수 없습니다."), 404
        return send_file(os.path.abspath(file_path), mimetype="image/jpeg")

    @app.post("/api/gold/<case_id>")
    def gold_label(case_id):
        data = body()
        label_id = gold.add_label(case_id, data.get("reviewer"), data.get("decision"), data.get("kreamId"), data.get("rationale"),
                                  data.get("startedAt"), data.get("endedAt"), data.get("seconds"), data.get("hiddenSeconds"),
                                  data.get("interruptions"))
        return jsonify(ok=True, label=label_id, origin=gold.origin_now(), status=gold.status())

    @app.get("/api/gold/report")
    def gold_report():
        return jsonify(report=gold_eval.evaluate(), timing=gold.timings()[-30:][::-1])

    @app.get("/api/export/gold-labels.csv")
    def export_gold_labels():
        return send_file(io.BytesIO(gold.labels_csv().encode("utf-8")), download_name="gold_labels.csv", as_attachment=True, mimetype="text/csv")

    @app.get("/api/export/review-timing.csv")
    def export_review_timing():
        return send_file(io.BytesIO(gold.timing_csv().encode("utf-8")), download_name="review_timing.csv", as_attachment=True, mimetype="text/csv")

    @app.get("/api/export/review-log.csv")
    def export_review_log():
        """Every human decision with its timer, for the review-time measurement."""
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["id", "createdAt(UTC)", "goodsNo", "action", "kreamId", "mode", "reviewSeconds", "suggestionDecision",
                         "suggestionKreamId", "previousKreamId", "previousMethod", "note"])
        for r in store.reviews():
            writer.writerow([exports._csv_safe(r.get(k)) for k in ("id", "createdAt", "goodsNo", "action", "kreamId", "mode", "reviewSeconds",
                                                                 "suggestionDecision", "suggestionKreamId", "previousKreamId",
                                                                 "previousMethod", "note")])
        return send_file(io.BytesIO(("\ufeff" + out.getvalue()).encode("utf-8")), download_name="review_log.csv",
                         as_attachment=True, mimetype="text/csv")

    # ---------------------------------------------------------- exports
    def export_rows():
        ids = _ids(request.args.get("ids", "").split(",")) if request.args.get("ids") else None
        rows = cmp.build(ids, include_kream_only=request.args.get("kream_only", "1") == "1")
        if not rows:
            raise CrawlError("내보내기", "prerequisite", "다운로드할 결과가 없습니다. 먼저 수집을 실행해 주세요.")
        return rows

    @app.get("/api/export/comparison.xlsx")
    def export_xlsx():
        return send_file(exports.comparison_workbook(export_rows()), download_name="shop_comparison.xlsx", as_attachment=True, mimetype=XLSX)

    @app.get("/api/export/price-check.xlsx")
    def export_price_check():
        """Per-size price comparison with thumbnails and highlights (last collected batch unless ids are given)."""
        from shop_comparator.matching import match_review
        ids = _ids(request.args.get("ids", "").split(",")) if request.args.get("ids") else \
            (store.get_setting("last_collection") or {}).get("goods") or []
        if not ids or not cmp.build(ids, include_kream_only=False):
            raise CrawlError("내보내기", "prerequisite", "다운로드할 결과가 없습니다. 먼저 수집을 실행해 주세요.")
        match_review.fill_match_images(ids, log=lambda text: L.log(text, level="warning", stage="export"))
        return send_file(match_review.export_xlsx(ids, stream=io.BytesIO()), download_name="price_check.xlsx",
                         as_attachment=True, mimetype=XLSX)

    @app.get("/api/export/comparison.csv")
    def export_csv():
        return send_file(exports.comparison_csv(export_rows()), download_name="shop_comparison.csv", as_attachment=True, mimetype="text/csv")

    @app.get("/api/export/gui.csv")
    def export_gui_csv():
        source = "poizon" if request.args.get("source") == "poizon" else "kream"
        return send_file(exports.gui_comparison_csv(export_rows(), source), as_attachment=True, mimetype="text/csv",
                         download_name=f"musinsa_{source}_comparison.csv")

    @app.get("/api/export/bid.xlsx")
    def export_bid():
        try:
            margin = float(request.args.get("margin", "10"))
        except ValueError:
            raise ValueError("마진율에 유효한 숫자를 입력하세요.")
        if not -100 < margin <= 1000:
            raise ValueError("마진율 범위를 확인해 주세요.")
        quantity = _int(request.args.get("quantity", "1"), "수량", 1, 9999)
        deadline = request.args.get("deadline", "무기한")
        if deadline not in DEADLINES:
            raise ValueError("입찰기한을 선택해 주세요.")
        ids = _ids(request.args.get("ids", "").split(",")) if request.args.get("ids") else None
        files = exports.bid_workbooks(exports.bid_rows(margin, quantity, deadline, ids))
        L.log(f"입찰 엑셀 생성: 파일 {len(files)}개", stage="export")
        if len(files) == 1:
            return send_file(files[0][1], download_name=files[0][0], as_attachment=True, mimetype=XLSX)
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in files:
                archive.writestr(name, data.getvalue())
        bundle.seek(0)
        return send_file(bundle, download_name="kream_bid_sheets.zip", as_attachment=True, mimetype="application/zip")

    # --------------------------------------------------------- settings
    @app.get("/api/settings")
    def settings_get():
        url = store.get_setting("webhook_url", "")
        return jsonify(webhookSet=bool(url), webhookMasked=mask_webhook(url), alertMargin=store.get_setting("alert_margin", 0),
                       alertBasis=store.get_setting("alert_basis", "kream_buy_minus_musinsa"), alertBases=pipeline.ALERT_BASES,
                       brands=brand_list(), trustUnverified=cmp.trust_unverified())

    @app.post("/api/settings")
    def settings_set():
        data = body()
        if "webhook_url" in data:
            url = str(data["webhook_url"] or "").strip()
            parsed = urlparse(url)
            if url and not (parsed.scheme == "https" and parsed.hostname in {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
                            and parsed.path.startswith("/api/webhooks/")):
                raise ValueError("디스코드 웹훅 URL(https://discord.com/api/webhooks/...)만 입력할 수 있습니다.")
            store.set_setting("webhook_url", url)
        if "alert_basis" in data:
            if data["alert_basis"] not in pipeline.ALERT_BASES:
                raise ValueError("알림 기준을 선택해 주세요.")
            store.set_setting("alert_basis", data["alert_basis"])
        if "alert_margin" in data:
            store.set_setting("alert_margin", _int(data["alert_margin"] or 0, "알림 마진", 0, 10**9))
        return jsonify(ok=True)

    @app.post("/api/settings/test-webhook")
    def webhook_test():
        url = store.get_setting("webhook_url", "")
        if not url:
            raise ValueError("웹훅 URL을 먼저 저장해 주세요.")
        sent = webhook.send_discord_notification(url, "Shop Comparator 테스트 알림입니다.")
        L.log("디스코드 테스트 알림 " + ("전송" if sent else "전송 실패"), stage="notify", level="info" if sent else "warning")
        return jsonify(ok=bool(sent)), 200 if sent else 502

    # ------------------------------------------------------- monitoring
    @app.get("/api/monitors")
    def monitors_get():
        return jsonify(monitors={k: m.snapshot() for k, m in monitors.items()}, auto=auto.snapshot())

    @app.post("/api/monitors/<kind>/<action>")
    def monitor_action(kind, action):
        if kind not in monitors:
            raise ValueError("지원하지 않는 모니터링입니다.")
        monitor, data = monitors[kind], body()
        label = monitoring.KINDS[kind]["label"]
        if action == "add":
            ids = [str(p["goodsNo"]) for p in store.products()] if data.get("current") else _ids(data.get("ids") or [])
            if not ids:
                raise ValueError("메인 목록에 상품이 없습니다." if data.get("current") else "유효한 상품 ID(숫자)를 입력하세요.")
            return start("monitor-add", f"{label} 모니터링 상품 추가", ["musinsa"], lambda ctx: ctx.note("added", monitor.add(ids)))
        if action == "remove":
            monitor.remove(_ids(data.get("ids") or []))
        elif action == "start":
            monitor.start(_int(data.get("interval", monitor.meta["interval"]), "모니터링 주기", 1, 86400))
        elif action == "stop":
            monitor.stop()
        else:
            raise ValueError("지원하지 않는 동작입니다.")
        return jsonify(ok=True)

    @app.post("/api/auto/<action>")
    def auto_action(action):
        data = body()
        if action == "stop":
            auto.stop()
            L.log("자동 이벤트 모드 중지를 요청했습니다.", stage="auto-event")
            return jsonify(ok=True)
        if action != "start":
            raise ValueError("지원하지 않는 동작입니다.")
        if site_login.status()["musinsa"]["state"] != "logged_in":
            raise ValueError("먼저 무신사 로그인을 완료해 주세요.")
        seed = None
        if data.get("seed") in ("ranking", "category", "brand"):
            seed = validate_settings({**data, "mode": data["seed"], "replace_list": True})
        auto.start(data.get("mode"), data.get("day", 300), data.get("night", 1800), seed)
        return jsonify(ok=True)

    # ------------------------------------------------------------- logs
    @app.get("/api/logs")
    def logs():
        try:
            after = int(request.args.get("after", "0"))
        except ValueError:
            after = 0
        events = L.recent(after, request.args.get("job") or None, request.args.get("shop") or None,
                          request.args.get("level") or None, request.args.get("q") or None)
        return jsonify(events=events, last=L.last_seq(), file=os.path.relpath(L.LOG_FILE, get_persistent_path(".")))

    return app


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        from shop_comparator.matching.product_matcher import load_local_env
        load_local_env()
    except Exception:
        pass
    store.init()
    store.interrupt_stale_jobs()
    L.log(f"웹 서버 시작: http://127.0.0.1:{args.port}", stage="server")
    create_app().run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()

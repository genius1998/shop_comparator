"""Check the review policy end to end through the real local web app.

    python scripts/verify_review_policy.py [--save]

What is real here
  A Flask server on 127.0.0.1:8798, HTTP requests with the CSRF token, the job manager, and SQLite files on disk.
What is replayed (labelled "저장 결과 재생" in the report)
  The shop and the model are not contacted.  The KREAM search returns the pick that was stored earlier for the
  same product (kream id, method, model-reported value, reason), and price requests return the stored quotes.
What is automation
  The approvals and rejections are sent by this script.  They are not human reviews and are not counted as such.

The working database is copied with SQLite's backup API into integration_check/ and only the copy is used;
its SHA-256 is compared before and after to show the original was not changed.
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "integration_check")
PORT = 8798
BASE = f"http://127.0.0.1:{PORT}"


def sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare():
    """Copy the database, drop secrets, and pick the products each check uses."""
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    os.makedirs(os.path.join(WORK, "match_review", "images"))
    source = sqlite3.connect(f"file:{os.path.join(ROOT, 'musinsa_data.sqlite')}?mode=ro", uri=True)
    copy = sqlite3.connect(os.path.join(WORK, "musinsa_data.sqlite"))
    source.backup(copy)
    source.close()
    copy.row_factory = sqlite3.Row
    for table in ("cookies", "site_sessions", "job_runs", "job_items", "vlm_suggestions", "match_reviews", "monitor_items"):
        try:
            copy.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            pass
    copy.execute("DELETE FROM web_settings WHERE key LIKE '%webhook%' OR key LIKE '%alert%'")
    quoted = {r[0] for r in copy.execute("SELECT DISTINCT kreamId FROM kream_quotes")}
    priced = {r[0] for r in copy.execute("SELECT DISTINCT goodsNo FROM musinsa_variants WHERE finalPrice IS NOT NULL")}

    def rows(method):
        found = [dict(r) for r in copy.execute("""SELECT c.* FROM kream_search_cache c JOIN products p ON p.goodsNo = c.musinsa_goods_no
                                                  WHERE c.match_method = ? AND c.match_verified = 1 ORDER BY c.musinsa_goods_no""", (method,))]
        return [r for r in found if str(r["kream_product_id"]).split(".")[0] in quoted and r["musinsa_goods_no"] in priced]

    ai, coded, local = rows("openai"), rows("model_code"), rows("local")
    if len(ai) < 5 or not coded:
        sys.exit("이 DB에는 확인에 쓸 저장 결과(openai 5건, model_code 1건)가 부족합니다.")
    roles = {"suggest_approve": ai[0], "suggest_reject": ai[1], "legacy_keep": ai[2], "legacy_approve": ai[3], "legacy_reject": ai[4],
             "gui": ai[5] if len(ai) > 5 else None, "rule": local[0] if local else None, "model_code": coded[0], "manual": coded[1]}
    # products whose stored pick is replayed as a NEW search result start without a match
    for role in ("suggest_approve", "suggest_reject", "rule", "gui", "manual"):
        if roles.get(role):
            goods_no = roles[role]["musinsa_goods_no"]
            copy.execute("DELETE FROM kream_search_cache WHERE musinsa_goods_no = ?", (goods_no,))
            copy.execute("UPDATE products SET kream_product_id = NULL, kream_product_url = NULL WHERE goodsNo = ?", (goods_no,))
    copy.execute("INSERT OR REPLACE INTO web_settings (key, value) VALUES ('last_collection', ?)",
                 (json.dumps({"goods": [r["musinsa_goods_no"] for r in roles.values() if r], "at": "2026-10-10T00:00:00+00:00"}),))
    copy.commit()
    copy.close()
    with open(os.path.join(WORK, "match_review", "candidates.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    return roles


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()
    original = os.path.join(ROOT, "musinsa_data.sqlite")
    before = sha(original)
    roles = prepare()
    os.chdir(WORK)
    sys.path.insert(0, ROOT)
    for key in list(os.environ):
        if key.startswith(("MUSINSA_", "KREAM_", "POIZON_", "OPENAI_")):
            del os.environ[key]
    os.environ.update(SHOP_SKIP_DOTENV="1", SHOP_AUTOMATION="1",
                      SHOP_DEMO_LABEL="통합 확인 · 저장 결과 재생 · 네트워크 미호출 · 승인/거절은 자동화 조작")
    import requests
    from werkzeug.serving import make_server
    from shop_comparator.core.crawl_errors import CrawlError
    from shop_comparator.matching import match_review
    from shop_comparator.services import pipeline
    from shop_comparator.services.jobs import JobContext
    from shop_comparator.shops import kream_crawler, site_login
    from shop_comparator.storage import database, store
    from shop_comparator.web import web_app

    stored = {r["musinsa_goods_no"]: r for r in roles.values() if r}
    calls = {"search": [], "quotes": []}

    def replay_search(keyword, brand, serial_number, cookies, image_url=None):
        goods_no = replay_search.current
        calls["search"].append(goods_no)
        row = stored.get(goods_no)
        if not row:
            return None
        return kream_crawler.KreamSearchResult(int(str(row["kream_product_id"]).split(".")[0]), match_verified=True,
                                               match_method=row["match_method"], match_confidence=row["match_confidence"],
                                               match_reason=row["match_reason"], kream_brand=row["kream_brand"],
                                               kream_product_name=row["kream_product_name"], kream_image_url=row["kream_image_url"])

    real_match_products = pipeline.match_products

    def tracked_match_products(products, ctx, force=False):
        for product in products:                       # the replay needs to know which product is being searched
            replay_search.current = str(product["goodsNo"])
            real_match_products([product], ctx, force)

    def replay_quotes(kream_id, token=None):
        calls["quotes"].append(str(kream_id))
        rows = store.kream_quotes(kream_id)
        if not rows:
            raise CrawlError("KREAM", "schema", "저장된 시세가 없습니다.")
        return [{"size": r["size"], "instantBuy": r["instantBuy"], "instantBuyDelivery": r["instantBuyDelivery"], "instantSell": r["instantSell"],
                 "bidOnly": bool(r["bidOnly"]), "candidates": []} for r in rows]

    @contextmanager
    def no_browser(*a, **k):
        yield mock.Mock(), None

    def refuse(*a, **k):
        raise CrawlError("통합 확인", "prerequisite", "쇼핑몰에 접속하지 않습니다.")

    original_wait = JobContext.wait
    for patch in (mock.patch.object(pipeline, "kream_session", no_browser), mock.patch.object(pipeline, "musinsa_session", refuse),
                  mock.patch.object(pipeline, "match_products", tracked_match_products),
                  mock.patch.object(pipeline, "_blacklist", lambda: set()), mock.patch.object(pipeline, "_blacklist_add", lambda goods_no: None),
                  mock.patch.object(pipeline, "_notify_margin", lambda *a, **k: None),
                  mock.patch.object(kream_crawler, "search_product", replay_search),
                  mock.patch.object(pipeline.kream_api, "fetch_quotes", replay_quotes),
                  mock.patch.object(match_review, "save_image", lambda url, path: False),
                  mock.patch.object(site_login, "login_musinsa", refuse), mock.patch.object(site_login, "login_kream", refuse),
                  mock.patch.object(database, "save_kream_size_prices", lambda *a, **k: None),
                  mock.patch.object(database, "save_kream_size_prices_v2", lambda *a, **k: None),
                  mock.patch.object(JobContext, "wait", lambda self, seconds: original_wait(self, min(seconds, 0.05)))):
        patch.start()

    store.init()
    app = web_app.create_app()
    server = make_server("127.0.0.1", PORT, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    http = requests.Session()
    token = __import__("re").search(r'name="csrf-token" content="([^"]+)"', http.get(BASE + "/").text)[1]

    def post(path, body=None):
        return http.post(BASE + path, json=body or {}, headers={"X-CSRF-Token": token})

    def job(kind, body):
        response = post(f"/api/jobs/{kind}", body)
        assert response.status_code == 202, response.text
        job_id = response.json()["job"]
        for _ in range(600):
            found = next(j for j in http.get(BASE + "/api/state").json()["jobs"] if j["id"] == job_id)
            if found["state"] not in ("running", "stopping"):
                return found
            time.sleep(0.1)
        raise RuntimeError("job did not finish")

    def view(goods_no):
        """What the web shows for one product: match status and how many sizes carry a KREAM price."""
        product = next(p for p in http.get(BASE + "/api/products").json()["products"] if p["goodsNo"] == goods_no)
        match = next(m for m in http.get(BASE + "/api/matches").json()["matches"] if m["goodsNo"] == goods_no)
        return {"status": match["status"], "kind": match["kind"], "usable": match["usable"], "method": match["method"],
                "originMethod": match.get("originMethod") or "", "kreamId": match["kreamId"],
                "sizesWithKreamPrice": sum(1 for s in product["sizes"] if s.get("kreamBuy") is not None)}

    def suggestions(goods_no):
        return store._rows("SELECT source, decision, kreamId, status FROM vlm_suggestions WHERE goodsNo = ?", (goods_no,))

    checks = []

    def check(name, observed, expected, data="저장 결과 재생"):
        checks.append({"check": name, "pass": observed == expected, "expected": expected, "observed": observed, "data": data})

    g = {role: (row["musinsa_goods_no"] if row else None) for role, row in roles.items()}
    kream = {role: (str(row["kream_product_id"]).split(".")[0] if row else None) for role, row in roles.items()}

    # 1. AI suggestion -> not in the price comparison before approval
    first = job("match", {"ids": [g["suggest_approve"], g["suggest_reject"]]})
    check("AI 제안 생성: 매칭 작업이 모델의 선택을 등록하지 않고 제안으로 저장", (first["summary"].get("suggested"), first["summary"].get("matched")), (2, None))
    check("AI 제안 생성: 제안 1건, 출처 matcher:openai", [(s["source"], s["decision"], s["kreamId"]) for s in suggestions(g["suggest_approve"])],
          [("matcher:openai", "same", kream["suggest_approve"])])
    v = view(g["suggest_approve"])
    check("승인 전: 매칭 없음, 가격 비교에 KREAM 가격 0개", (v["status"], v["usable"], v["sizesWithKreamPrice"]), ("매칭 없음", False, 0))
    price_job = job("kream", {"ids": [g["suggest_approve"]]})
    check("승인 전: 가격 수집 작업도 이 상품의 KREAM 시세를 요청하지 않음", (kream["suggest_approve"] in calls["quotes"], price_job["summary"].get("ok")), (False, None))

    # 5. repeat: no duplicate suggestion, no second search
    searches = len(calls["search"])
    again = job("match", {"ids": [g["suggest_approve"], g["suggest_reject"]]})
    check("반복 실행: 검색(모델 호출 경로) 재호출 0회, 제안 중복 없음",
          (len(calls["search"]) - searches, again["summary"].get("awaitingReview"), len(suggestions(g["suggest_approve"]))), (0, 2, 1))
    again_price = job("kream", {"ids": [g["suggest_approve"]]})
    check("반복 실행: 가격 수집의 누락 매칭 검색 경로도 재호출 0회", (len(calls["search"]) - searches, again_price["summary"].get("awaitingReview")), (0, 1))

    # 2. approve -> used
    response = post(f"/api/review/{g['suggest_approve']}", {"action": "approve", "kreamId": kream["suggest_approve"], "mode": "assisted"})
    check("승인 요청 성공", response.status_code, 200, "자동화 조작")
    v = view(g["suggest_approve"])
    check("승인 후: 검수 승인, 가격 비교에 KREAM 가격 반영", (v["status"], v["kind"], v["usable"], v["sizesWithKreamPrice"] > 0), ("검수 승인", "user", True, True),
          "저장 결과 재생 + 자동화 조작")

    # 3. reject -> stays out
    post(f"/api/review/{g['suggest_reject']}", {"action": "reject", "kreamId": kream["suggest_reject"], "note": "통합 확인용 자동 거절"})
    v = view(g["suggest_reject"])
    check("제안 거절 후: 가격 비교 제외", (v["usable"], v["sizesWithKreamPrice"]), (False, 0), "저장 결과 재생 + 자동화 조작")
    state = next(i["state"] for i in http.get(BASE + "/api/review").json()["items"] if i["goodsNo"] == g["suggest_reject"])
    check("제안 거절 후: 검수 화면 상태 rejected", state, "rejected", "자동화 조작")

    # 4. matches a model registered earlier
    v = view(g["legacy_keep"])
    check("기존 AI 등록: 미검수로 분류, 행은 그대로, 가격 비교 제외", (v["status"], v["kind"], v["usable"], v["kreamId"], v["sizesWithKreamPrice"]),
          ("AI 등록(미검수)", "ai_unreviewed", False, kream["legacy_keep"], 0))
    job("kream", {"ids": [g["legacy_keep"]]})
    check("기존 AI 등록: 가격 수집 작업이 시세를 요청하지 않음", kream["legacy_keep"] in calls["quotes"], False)
    post(f"/api/review/{g['legacy_approve']}", {"action": "approve", "kreamId": kream["legacy_approve"]})
    v = view(g["legacy_approve"])
    check("기존 AI 등록 승인 후: 반영, 원래 출처 openai 보존", (v["status"], v["usable"], v["originMethod"], v["sizesWithKreamPrice"] > 0),
          ("검수 승인", True, "openai", True), "저장 결과 재생 + 자동화 조작")
    post(f"/api/review/{g['legacy_reject']}", {"action": "reject", "kreamId": kream["legacy_reject"], "note": "통합 확인용 자동 거절"})
    v = view(g["legacy_reject"])
    kept = store._rows("SELECT kream_product_id, match_verified, review_state, origin_method FROM kream_search_cache WHERE musinsa_goods_no = ?", (g["legacy_reject"],))
    check("기존 AI 등록 거절 후: 제외, 행과 출처는 보존", (v["status"], v["usable"], v["sizesWithKreamPrice"], kept[0]["review_state"], kept[0]["origin_method"]),
          ("검수 거절", False, 0, "rejected", "openai"), "저장 결과 재생 + 자동화 조작")

    # 6. origin of rule and manual matches
    v = view(g["model_code"])
    check("규칙 매칭(모델번호 일치): 출처 model_code 그대로, 사용", (v["status"], v["kind"], v["method"], v["usable"]), ("모델번호 일치", "rule", "model_code", True))
    if g["rule"]:
        rule_job = job("match", {"ids": [g["rule"]]})
        v = view(g["rule"])
        check("규칙 매칭(local): 저장된 규칙 판정을 재생하면 바로 등록, 출처 local", (rule_job["summary"].get("matched"), v["status"], v["kind"], v["method"], v["usable"]),
              (1, "규칙 확정", "rule", "local", True))
    else:
        checks.append({"check": "규칙 매칭(local)", "pass": None, "expected": "저장된 local 판정", "observed": "이 DB에 조건에 맞는 local 행이 없어 미실행", "data": "미실행"})
    post("/api/matches", {"goodsNo": g["manual"], "kream": f"https://kream.co.kr/products/{kream['manual']}"})
    v = view(g["manual"])
    check("직접 지정: 출처 manual, 사용", (v["status"], v["kind"], v["method"], v["usable"]), ("직접 지정", "user", "manual", True), "자동화 조작")
    v = view(g["suggest_approve"])
    check("출처 구분: 사람 승인은 human_review로 따로 표시", (v["kind"], v["method"]), ("user", "human_review"), "자동화 조작")

    # 7. an unreviewed AI match written the way the desktop GUI writes it
    if g["gui"]:
        row = roles["gui"]
        gui_result = kream_crawler.KreamSearchResult(int(kream["gui"]), match_verified=True, match_method="openai", match_confidence=row["match_confidence"],
                                                     match_reason=row["match_reason"], kream_product_name=row["kream_product_name"])
        database.cache_kream_search(g["gui"], gui_result, f"https://kream.co.kr/products/{kream['gui']}")     # same calls as desktop/gui.py
        database.update_product_kream_id(g["gui"], kream["gui"])
        v = view(g["gui"])
        check("GUI에서 들어온 AI 매칭: 웹에서는 미검수, 가격 비교 제외", (v["status"], v["usable"], v["sizesWithKreamPrice"]), ("AI 등록(미검수)", False, 0))

    state_counts = http.get(BASE + "/api/state").json()["counts"]
    metrics = http.get(BASE + "/api/review").json()["metrics"]
    from shop_comparator.matching import gold
    human_reviews = [r for r in gold.timings() if r["origin"] == "human_ui"]
    server.shutdown()
    after = sha(original)
    check("운영 DB 파일이 바뀌지 않음 (SHA-256 전후 비교)", before == after, True, "실제 파일")
    check("사람 검수 기록 0건 (자동화 기록은 사람 검수로 세지 않음)", len(human_reviews), 0, "실제 파일")
    report = {
        "ranAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "server": BASE, "database": "integration_check/musinsa_data.sqlite (운영 DB의 사본)",
        "replayed": "KREAM 검색 결과(과거에 저장된 선택·방식·근거)와 KREAM 시세. 쇼핑몰과 모델을 호출하지 않았다.",
        "automation": "승인·거절·직접 지정은 이 스크립트가 HTTP로 보낸 것이다. 사람 검수가 아니다.",
        "roles": {role: {"goodsNo": g[role], "kreamId": kream[role]} for role in roles if roles[role]},
        "calls": {"search": len(calls["search"]), "quotes": len(calls["quotes"])},
        "appCounts": state_counts, "reviewMetrics": {k: metrics[k] for k in ("products", "aiUnreviewed", "needsReview", "approved", "rejected", "decisions")},
        "automationReviewRecords": len(gold.timings()), "humanReviewRecords": len(human_reviews),
        "passed": sum(1 for c in checks if c["pass"]), "failed": sum(1 for c in checks if c["pass"] is False),
        "notRun": sum(1 for c in checks if c["pass"] is None), "checks": checks,
    }
    for c in checks:
        print({True: "PASS", False: "FAIL", None: "SKIP"}[c["pass"]], "|", c["check"], "|", c["observed"], "|", c["data"])
    print(f"통과 {report['passed']} · 실패 {report['failed']} · 미실행 {report['notRun']} · 검색 호출 {report['calls']['search']}회 · 사람 검수 기록 {report['humanReviewRecords']}건")
    if args.save:
        out = os.path.join(ROOT, "docs", "evaluation", "integration")
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "policy_check.json"), "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
    sys.exit(1 if report["failed"] else 0)


if __name__ == "__main__":
    main()

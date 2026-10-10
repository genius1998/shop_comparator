"""VLM review flow and failure recovery (mock data; no shop and no paid API is contacted)."""
import csv
import io
import json
import os
import re
import unittest
from unittest import mock

from helpers import FakeContext, TempDbTestCase, fake_kream_session, seed_product
from shop_comparator.core.crawl_errors import CrawlError
from shop_comparator.matching import match_review, vlm_match
from shop_comparator.services import comparison_service as cmp
from shop_comparator.services import pipeline, vlm_review
from shop_comparator.services.jobs import JobContext, JobManager
from shop_comparator.storage import store
from shop_comparator.web.web_app import create_app


def candidates(goods_no, *items, collected="2026-10-09T06:00:00+00:00", images=True, serial="SHOP-01"):
    """Write one product's candidate list (and empty image files) into the temp match_review folder."""
    data = match_review.load()
    folder = os.path.join(match_review.IMAGES, goods_no)
    os.makedirs(folder, exist_ok=True)
    entry = {"goodsNo": goods_no, "brand": "브랜드", "name": "상품", "serial": serial, "collectedAt": collected,
             "musinsaImage": f"images/{goods_no}/musinsa.jpg" if images else None, "candidates": []}
    if images:
        open(os.path.join(folder, "musinsa.jpg"), "wb").close()
    for kream_id, model in items:
        if images:
            open(os.path.join(folder, f"kream_{kream_id}.jpg"), "wb").close()
        entry["candidates"].append({"kreamId": kream_id, "brand": "K", "name": f"후보 {kream_id}", "model": model,
                                    "image": f"images/{goods_no}/kream_{kream_id}.jpg" if images else None})
    data[goods_no] = entry
    match_review.save(data)


def stored(**verdicts):
    with open(vlm_match.RESULTS, "w", encoding="utf-8") as f:
        json.dump(verdicts, f, ensure_ascii=False)


def verdict(decision, kream_id="", confidence=0.95, at="2026-10-09T07:00:00+00:00"):
    return {"decision": decision, "kream_id": kream_id, "confidence": confidence, "reason": "형태와 색상이 같습니다.", "at": at, "seconds": 2.5,
            "usage": {"model": "gpt-5.4-mini-test", "input_tokens": 900, "output_tokens": 120}}


class SuggestionTests(TempDbTestCase):
    def setUp(self):
        super().setUp()
        for goods_no in ("1", "2", "3", "4", "5", "6"):
            seed_product(goods_no, f"상품 {goods_no}")
        candidates("1", ("100", ""), ("101", ""))
        candidates("2", ("200", ""))
        candidates("3", ("300", ""))
        candidates("4", ("400", ""), collected="2026-10-09T09:00:00+00:00")
        candidates("5", ("500", ""), images=False)
        # product 6 has no candidates at all

    def suggestions(self):
        return {k: (v["status"], v["decision"], v["kreamId"], v["errorKind"]) for k, v in store.latest_suggestions().items()}

    def test_stored_verdicts_become_suggestions_and_bad_ones_are_not_offered(self):
        stored(**{"1": verdict("same", "101"), "2": verdict("same", "999"), "3": {"decision": "maybe", "kream_id": ""},
                  "4": verdict("same", "400"), "5": verdict("same", "500")})
        ctx = FakeContext()
        vlm_review.review_products(["1", "2", "3", "4", "5", "6"], ctx, "stored")
        self.assertEqual(self.suggestions(), {
            "1": ("valid", "same", "101", None),
            "2": ("invalid", None, None, "unknown_candidate"),      # an id outside the candidate list
            "3": ("invalid", None, None, "no_stored_verdict"),      # wrong shape is not a verdict
            "4": ("invalid", None, None, "stale"),                  # judged before the candidates were collected again
            "5": ("error", None, None, "image_missing"),
            "6": ("error", None, None, "no_candidates"),
        })
        self.assertEqual((ctx.summary["ok"], ctx.summary["failed"], ctx.summary["apiCalls"]), (1, 5, 0))
        self.assertEqual(store.job_item_summary("test-job")["vlm"], {"pending": 0, "ok": 1, "failed": 5, "skipped": 0})
        # a suggestion, however confident, never becomes a match by itself
        self.assertEqual(cmp.match_info(store.match_for("1"))["status"], "매칭 없음")
        queue = {i["goodsNo"]: i for i in vlm_review.queue(["1", "2", "5"])}
        self.assertEqual((queue["1"]["state"], queue["1"]["suggestion"]["kreamId"]), ("pending", "101"))
        self.assertEqual((queue["2"]["state"], queue["2"]["suggestion"], queue["2"]["lastAttempt"]["errorKind"]),
                         ("no_suggestion", None, "unknown_candidate"))

    def test_running_again_does_not_duplicate_and_a_failed_attempt_keeps_the_earlier_verdict(self):
        stored(**{"1": verdict("same", "101")})
        vlm_review.review_products(["1"], FakeContext(), "stored")
        second = FakeContext()
        vlm_review.review_products(["1"], second, "stored")
        self.assertEqual(second.summary.get("alreadyJudged"), 1)
        self.assertEqual(len(store._rows("SELECT id FROM vlm_suggestions WHERE goodsNo = '1'")), 1)
        stored(**{"1": {"decision": "broken"}})
        vlm_review.review_products(["1"], FakeContext(), "stored", force=True)
        self.assertEqual(len(store._rows("SELECT id FROM vlm_suggestions WHERE goodsNo = '1'")), 2)
        item = vlm_review.queue(["1"])[0]
        self.assertEqual((item["suggestion"]["kreamId"], item["lastAttempt"]["status"]), ("101", "invalid"))

    def test_a_verdict_for_an_older_candidate_list_is_marked_stale(self):
        stored(**{"1": verdict("same", "101")})
        vlm_review.review_products(["1"], FakeContext(), "stored")
        candidates("1", ("100", ""), ("101", ""), ("102", ""))            # the search was repeated and found one more
        item = vlm_review.queue(["1"])[0]
        self.assertEqual((item["stale"], item["suggestion"], item["state"]), (True, None, "no_suggestion"))

    def test_live_run_needs_a_call_limit_and_stops_at_it(self):
        with self.assertRaises(ValueError):
            vlm_review.review_products(["1"], FakeContext(), "live")
        with self.assertRaises(ValueError):
            vlm_review.review_products(["1"], FakeContext(), "live", max_calls=0)
        calls = []

        def judge(entry):
            calls.append(entry["goodsNo"])
            if entry["goodsNo"] == "2":
                raise RuntimeError("OpenAI API 500")
            return {"decision": "same", "kream_id": entry["candidates"][0]["kreamId"], "confidence": 0.9, "reason": "r"}, \
                {"model": "m", "input_tokens": 10, "output_tokens": 5}

        ctx = FakeContext()
        vlm_review.review_products(["1", "2", "3", "4"], ctx, "live", max_calls=3, judge=judge)
        self.assertEqual(calls, ["1", "2", "3"])                          # the fourth product is not sent
        self.assertEqual((ctx.summary["apiCalls"], ctx.summary["ok"], ctx.summary["failed"], ctx.summary["notRunLimit"]), (3, 2, 1, 1))
        self.assertEqual(store.job_item_summary("test-job")["vlm"], {"pending": 1, "ok": 2, "failed": 1, "skipped": 0})
        self.assertEqual(self.suggestions()["2"], ("error", None, None, "api"))
        self.assertEqual(pipeline.unfinished("test-job"), {"vlm": ["2", "4"]})

    def test_confidence_outside_zero_to_one_is_dropped_not_trusted(self):
        entry = match_review.load()["1"]
        self.assertEqual(vlm_review.validate(entry, {"decision": "same", "kream_id": 101, "confidence": 95, "reason": "x"}),
                         ("same", "101", None, "x"))
        self.assertEqual(vlm_review.validate(entry, {"decision": "different", "kream_id": "101", "confidence": 0.4, "reason": ""}),
                         ("different", "", 0.4, ""))
        for bad in ("text", {"decision": "same", "kream_id": ""}, {"decision": "same", "kream_id": "777"}, {"decision": "SAME"}):
            with self.assertRaises(vlm_review.InvalidAnswer):
                vlm_review.validate(entry, bad)


class DecisionTests(TempDbTestCase):
    def setUp(self):
        super().setUp()
        seed_product("1", "러너", sizes=(("10", "250", 90000), ("11", "260", 95000)))
        candidates("1", ("500", ""), ("501", ""))
        store.save_kream_quotes("500", [{"size": "250", "instantBuy": 100000, "instantBuyDelivery": "일반배송", "instantSell": 80000}])
        stored(**{"1": verdict("same", "500", confidence=0.99)})
        vlm_review.review_products(["1"], FakeContext(), "stored")

    def test_only_an_approval_makes_the_match_used_for_prices(self):
        self.assertFalse(any(r["kreamBuy"] for r in cmp.build(["1"])))                    # suggestion alone: no KREAM price is used
        review_id = vlm_review.decide("1", "approve", "500", "이미지 확인", seconds=12.3)
        info = cmp.match_info(store.match_for("1"))
        self.assertEqual((info["kreamId"], info["status"], info["usable"], info["confidence"]), ("500", "검수 승인", True, None))
        self.assertIn(f"#{review_id}", info["reason"])
        row = next(r for r in cmp.build(["1"]) if r["size"] == "250")
        self.assertEqual((row["musinsaPrice"], row["kreamBuy"], row["difference"]), (90000, 100000, 10000))
        review = store.reviews(["1"])[-1]
        self.assertEqual((review["action"], review["kreamId"], review["suggestionDecision"], review["suggestionKreamId"],
                          review["mode"], review["reviewSeconds"]), ("approve", "500", "same", "500", "assisted", 12.3))

    def test_reject_removes_the_registered_match_and_hold_changes_nothing(self):
        store.set_manual_match("1", "500", "자동 판별기", method="openai", confidence=0.83)
        vlm_review.decide("1", "hold", "500")
        self.assertEqual(cmp.match_info(store.match_for("1"))["kreamId"], "500")
        vlm_review.decide("1", "reject", "500", "색상이 다름")
        rejected = cmp.match_info(store.match_for("1"))
        self.assertEqual((rejected["status"], rejected["usable"], rejected["kreamId"], rejected["originMethod"]),
                         ("검수 거절", False, "500", "openai"))                            # kept with its source, not deleted
        self.assertFalse(any(r["kreamBuy"] for r in cmp.build(["1"])))
        history = store.reviews(["1"])
        self.assertEqual([(r["action"], r["previousKreamId"], r["previousMethod"]) for r in history],
                         [("hold", "500", "openai"), ("reject", "500", "openai")])
        self.assertEqual(vlm_review.queue(["1"])[0]["state"], "rejected")
        self.assertEqual(store.kream_quotes("500")[0]["instantBuy"], 100000)                   # collected prices are not deleted

    def test_invalid_decisions_are_refused_and_leave_no_record(self):
        for action, kream_id in (("approve", ""), ("approve", "999"), ("approve", "abc"), ("delete", "500")):
            with self.assertRaises(ValueError):
                vlm_review.decide("1", action, kream_id)
        with self.assertRaises(ValueError):
            vlm_review.decide("404", "hold")
        self.assertEqual(store.reviews(), [])
        vlm_review.decide("1", "hold", seconds=99999)                                          # absurd timer value is not stored
        self.assertIsNone(store.reviews()[-1]["reviewSeconds"])

    def test_conflict_with_the_model_code_rule_is_shown(self):
        seed_product("2", "에어포스 / CW2288-111", serial_number="CW2288-111")
        candidates("2", ("12831", "315122-111/CW2288-111"), ("900", "DD8959-100"), serial="CW2288-111")
        stored(**{"2": verdict("same", "900")})
        vlm_review.review_products(["2"], FakeContext(), "stored", force=True)
        item = vlm_review.queue(["2"])[0]
        self.assertEqual(item["rule"], "model_code")
        self.assertTrue(any("모델번호 규칙은 12831" in c for c in item["conflicts"]))

    def test_metrics_count_only_what_people_actually_did(self):
        before = vlm_review.metrics(["1"])
        self.assertEqual((before["vlmJudged"], before["needsReview"], before["approved"]), (1, 1, 0))
        self.assertEqual(before["reviewSeconds"], {"assisted": {"count": 0, "median": None}, "manual": {"count": 0, "median": None}})
        vlm_review.decide("1", "approve", "500", seconds=8.0)
        vlm_review.decide("1", "approve", "500", seconds=4.0, mode="manual")
        after = vlm_review.metrics(["1"])
        self.assertEqual((after["approved"], after["approvedWithVlm"], after["humanApprovedMatches"], after["needsReview"]), (1, 1, 1, 0))
        self.assertEqual(after["reviewSeconds"], {"assisted": {"count": 1, "median": 8.0}, "manual": {"count": 1, "median": 4.0}})
        self.assertEqual((after["inputTokens"], after["outputTokens"], after["vlmSecondsMedian"]), (900, 120, 2.5))


class ReviewApiTests(TempDbTestCase):
    def setUp(self):
        super().setUp()
        self.jobs = JobManager(persist=True)
        self.client = create_app(self.jobs).test_client()
        self.token = re.search(r'name="csrf-token" content="([^"]+)"', self.client.get("/").text)[1]
        seed_product("1", "러너")
        candidates("1", ("500", ""))
        stored(**{"1": verdict("same", "500")})
        store.set_setting("last_collection", {"goods": ["1"], "at": store.now()})

    def post(self, path, body=None):
        return self.client.post(path, json=body or {}, headers={"X-CSRF-Token": self.token})

    def test_review_flow_through_the_web_api(self):
        self.assertEqual(self.post("/api/jobs/vlm", {"mode": "live"}).status_code, 400)             # paid run without a limit
        self.assertEqual(self.post("/api/jobs/vlm", {"mode": "live", "max_calls": 0}).status_code, 400)
        started = self.post("/api/jobs/vlm", {"mode": "stored"})
        self.assertEqual(started.status_code, 202)
        job = self.jobs.wait(started.get_json()["job"], 5)
        self.assertEqual((job["state"], job["summary"]["ok"], job["summary"]["apiCalls"]), ("done", 1, 0))
        data = self.client.get("/api/review").get_json()
        item = data["items"][0]
        self.assertEqual((item["state"], item["suggestion"]["decision"], item["suggestion"]["source"]), ("pending", "same", "stored"))
        self.assertIn("확률이 아니", data["confidenceNote"])
        image = self.client.get(item["musinsaImage"])
        self.assertEqual(image.status_code, 200)
        image.close()
        for path in ("/api/review/image/1/..%2f..%2fmusinsa_data.sqlite", "/api/review/image/1/other.jpg", "/api/review/image/x/musinsa.jpg"):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.post("/api/review/1", {"action": "approve", "kreamId": "777"}).status_code, 400)
        self.assertEqual(self.post("/api/review/1", {"action": "approve", "kreamId": "500", "seconds": 6.5}).status_code, 200)
        self.assertEqual(self.client.get("/api/review").get_json()["items"][0]["state"], "approved")
        product = self.client.get("/api/products").get_json()["products"][0]
        self.assertEqual((product["match"]["status"], product["match"]["kreamId"]), ("검수 승인", "500"))
        log = list(csv.DictReader(io.StringIO(self.client.get("/api/export/review-log.csv").data.decode("utf-8-sig"))))
        self.assertEqual((log[0]["action"], log[0]["kreamId"], log[0]["reviewSeconds"], log[0]["mode"]), ("approve", "500", "6.5", "assisted"))

    def test_cancelled_review_can_be_finished_later_without_repeating_finished_products(self):
        for goods_no in ("2", "3"):
            seed_product(goods_no, f"상품 {goods_no}")
            candidates(goods_no, (goods_no + "00", ""))
        stored(**{g: verdict("same", k) for g, k in (("1", "500"), ("2", "200"), ("3", "300"))})
        store.set_setting("last_collection", {"goods": ["1", "2", "3"], "at": store.now()})
        real, seen = vlm_review.validate, []

        def stop_after_first(entry, answer):
            seen.append(entry["goodsNo"])
            if len(seen) == 1:
                self.jobs.stop()                                    # the user presses [작업 중단] while product 1 is handled
            return real(entry, answer)

        with mock.patch.object(vlm_review, "validate", side_effect=stop_after_first):
            job_id = self.post("/api/jobs/vlm", {"mode": "stored"}).get_json()["job"]
            self.assertEqual(self.jobs.wait(job_id, 5)["state"], "cancelled")
        items = self.client.get(f"/api/jobs/{job_id}/items").get_json()
        self.assertEqual((items["summary"]["vlm"], items["unfinished"]), ({"pending": 2, "ok": 1, "failed": 0, "skipped": 0}, {"vlm": ["2", "3"]}))
        history = next(j for j in self.client.get("/api/jobs/history").get_json()["jobs"] if j["id"] == job_id)
        self.assertEqual(history["unfinished"], 2)
        resumed = self.post(f"/api/jobs/{job_id}/resume")
        self.assertEqual(resumed.status_code, 202)
        again = self.jobs.wait(resumed.get_json()["job"], 5)
        self.assertEqual((again["state"], again["summary"]["ok"], again["summary"]["resumeOf"]), ("done", 2, job_id))
        self.assertEqual(len(store._rows("SELECT id FROM vlm_suggestions")), 3)             # one each, none twice
        self.assertEqual(self.post(f"/api/jobs/{resumed.get_json()['job']}/resume").status_code, 409)   # nothing left to redo
        self.assertEqual(self.post("/api/jobs/nope/resume").status_code, 404)


class PriceRecoveryTests(TempDbTestCase):
    """A timeout in the middle of KREAM price collection: finish later, no gaps, no duplicates, no lost prices."""

    def setUp(self):
        super().setUp()
        self.jobs = JobManager(persist=True)
        self.client = create_app(self.jobs).test_client()
        self.token = re.search(r'name="csrf-token" content="([^"]+)"', self.client.get("/").text)[1]
        for goods_no, kream_id in (("1", "501"), ("2", "502"), ("3", "503"), ("4", "504")):
            seed_product(goods_no, f"상품 {goods_no}")
            store.set_manual_match(goods_no, kream_id)
        store.save_kream_quotes("502", [{"size": "250", "instantBuy": 70000, "instantSell": 60000}])   # verified earlier
        store.set_setting("last_collection", {"goods": ["1", "2", "3", "4"], "at": store.now()})

    def post(self, path, body=None):
        return self.client.post(path, json=body or {}, headers={"X-CSRF-Token": self.token})

    def test_resume_fetches_only_failed_and_unreached_products(self):
        calls, down = [], {"502", "503", "504"}

        def fetch(kream_id, token=None):
            calls.append(kream_id)
            if kream_id in down:
                raise CrawlError("KREAM", "network", "연결 시간 초과")       # test fault injection: no real site is contacted
            return [{"size": "250", "instantBuy": 100000 + int(kream_id), "instantBuyDelivery": "일반배송", "instantSell": 90000, "bidOnly": False}]

        no_wait = mock.patch.object(JobContext, "wait", lambda self, seconds: self.check())
        with fake_kream_session(), no_wait, mock.patch.object(pipeline.kream_api, "fetch_quotes", side_effect=fetch), \
                mock.patch.object(pipeline, "match_products"), mock.patch.object(pipeline, "_notify_margin"):
            job_id = self.post("/api/jobs/kream", {}).get_json()["job"]
            first = self.jobs.wait(job_id, 10)
            self.assertEqual((first["state"], first["errorKind"]), ("failed", "network"))    # three in a row: the job stops itself
            self.assertEqual(calls, ["501", "502", "502", "503", "503", "504", "504"])       # one retry each, then stop
            summary = self.client.get(f"/api/jobs/{job_id}/items").get_json()
            self.assertEqual(summary["summary"]["kream"], {"pending": 0, "ok": 1, "failed": 3, "skipped": 0})
            # the price verified before the outage is still there and is told apart from the failed refresh
            self.assertEqual(store.kream_quotes("502")[0]["instantBuy"], 70000)
            states = {str(p["goodsNo"]): (p["kreamStatus"], bool(p["kreamError"])) for p in store.products()}
            self.assertEqual(states, {"1": ("ok", False), "2": ("error", True), "3": ("error", True), "4": ("error", True)})

            down.clear()
            calls.clear()
            resumed = self.post(f"/api/jobs/{job_id}/resume").get_json()["job"]
            second = self.jobs.wait(resumed, 10)
            self.assertEqual((second["state"], second["summary"]["ok"]), ("done", 3))
            self.assertEqual(calls, ["502", "503", "504"])                                   # product 1 is not requested again
        rows = store._rows("SELECT kreamId, size, COUNT(*) AS n FROM kream_quotes GROUP BY kreamId, size")
        self.assertEqual(sorted((r["kreamId"], r["n"]) for r in rows), [("501", 1), ("502", 1), ("503", 1), ("504", 1)])   # no duplicates
        self.assertEqual(store.kream_quotes("502")[0]["instantBuy"], 100502)                 # replaced by the fresh value
        self.assertEqual({p["kreamStatus"] for p in store.products()}, {"ok"})
        self.assertEqual(self.post(f"/api/jobs/{job_id}/resume").status_code, 202)           # the old ledger still lists them ...
        self.jobs.wait(self.client.get("/api/state").get_json()["jobs"][0]["id"], 10)


class MatcherPolicyTests(TempDbTestCase):
    """A model's judgement never registers a match: it becomes a suggestion that a person approves or rejects."""

    def setUp(self):
        super().setUp()
        from shop_comparator.shops import kream_crawler
        self.kream_crawler = kream_crawler
        seed_product("1", "러너", sizes=(("10", "250", 90000),))
        store.save_kream_quotes("900", [{"size": "250", "instantBuy": 100000, "instantBuyDelivery": "일반배송", "instantSell": 80000}])

    def found(self, kream_id=900, method="openai", confidence=0.93):
        return self.kream_crawler.KreamSearchResult(kream_id, match_verified=True, match_method=method, match_confidence=confidence,
                                                    match_reason="상품명과 이미지가 일치합니다.", kream_brand="K", kream_product_name="러너 블랙")

    def search(self, result):
        return mock.patch.object(self.kream_crawler, "search_product", return_value=result)

    def priced(self, goods_no="1"):
        return [r["kreamBuy"] for r in cmp.build([goods_no]) if r["kreamBuy"] is not None]

    def test_llm_pick_is_a_suggestion_until_a_person_approves_it(self):
        ctx = FakeContext()
        with self.search(self.found()), mock.patch.object(pipeline, "_blacklist_add") as blacklist:
            pipeline.match_products(store.products(goods_nos=["1"]), ctx)
        blacklist.assert_not_called()                                   # something was found: not "no match"
        self.assertEqual(ctx.summary, {"suggested": 1})
        self.assertIsNone(store.match_for("1"))                         # nothing registered
        self.assertEqual(store.counts()["matched"], 0)
        suggestion = store.latest_suggestions(["1"])["1"]
        self.assertEqual((suggestion["source"], suggestion["decision"], suggestion["kreamId"], suggestion["confidence"], suggestion["status"]),
                         ("matcher:openai", "same", "900", 0.93, "valid"))
        self.assertEqual(self.priced(), [])                             # before approval: not in the price comparison
        item = vlm_review.queue(["1"])[0]
        self.assertEqual((item["state"], item["suggestion"]["kreamId"], [c["kreamId"] for c in item["candidates"]]), ("pending", "900", ["900"]))

        with self.search(self.found()) as search:                       # running again neither asks the model nor duplicates
            again = FakeContext()
            pipeline.match_products(store.products(goods_nos=["1"]), again)
            search.assert_not_called()
        self.assertEqual(again.summary, {"awaitingReview": 1})
        self.assertEqual(len(store._rows("SELECT id FROM vlm_suggestions")), 1)

        vlm_review.decide("1", "approve", "900", seconds=5)
        info = cmp.match_info(store.match_for("1"))
        self.assertEqual((info["status"], info["kind"], info["usable"]), ("검수 승인", "user", True))
        self.assertEqual(self.priced(), [100000])                       # after approval: used
        self.assertEqual(store.counts()["matched"], 1)

    def test_rejected_suggestion_stays_out_of_the_price_comparison(self):
        with self.search(self.found()):
            pipeline.match_products(store.products(goods_nos=["1"]), FakeContext())
        vlm_review.decide("1", "reject", "900", "다른 색상")
        self.assertIsNone(store.match_for("1"))
        self.assertEqual(self.priced(), [])
        self.assertEqual(vlm_review.queue(["1"])[0]["state"], "rejected")
        self.assertEqual(store.kream_quotes("900")[0]["instantBuy"], 100000)   # collected prices are untouched

    def test_every_job_path_goes_through_the_same_policy(self):
        """KREAM matching, price collection with missing matches, and the full run."""
        no_wait = mock.patch.object(JobContext, "wait", lambda self, seconds: self.check())
        for run in (lambda ctx: pipeline.match_kream(["1"], ctx),
                    lambda ctx: pipeline.match_kream(["1"], ctx, force=True),
                    lambda ctx: pipeline.collect_kream(["1"], ctx, match_missing=True),
                    lambda ctx: pipeline.full({"mode": "selected", "ids": ["1"], "limit": 1, "price_mode": "none"}, ctx)):
            store._exec("DELETE FROM vlm_suggestions")
            with fake_kream_session(), no_wait, self.search(self.found()), mock.patch.object(pipeline, "_blacklist", return_value=set()), \
                    mock.patch.object(pipeline, "collect_musinsa", return_value=["1"]), \
                    mock.patch.object(pipeline.kream_api, "fetch_quotes", side_effect=AssertionError("no price request before approval")):
                ctx = FakeContext()
                run(ctx)
            self.assertIsNone(store.match_for("1"))
            self.assertEqual(ctx.summary.get("suggested"), 1)
            self.assertEqual(self.priced(), [])

    def test_rule_and_user_matches_are_registered_and_told_apart(self):
        with self.search(self.found(method="local", confidence=0.9)):
            pipeline.match_products(store.products(goods_nos=["1"]), FakeContext())
        info = cmp.match_info(store.match_for("1"))
        self.assertEqual((info["status"], info["kind"], info["usable"]), ("규칙 확정", "rule", True))
        seed_product("2", "부츠")
        store.set_manual_match("2", "901")
        seed_product("3", "로퍼")
        store.set_manual_match("3", "902", method="model_code")
        kinds = {g: cmp.match_info(store.match_for(g))["kind"] for g in ("1", "2", "3")}
        self.assertEqual(kinds, {"1": "rule", "2": "user", "3": "rule"})

    def test_matches_registered_by_a_model_earlier_are_kept_but_wait_for_review(self):
        for goods_no, kream_id, method in (("1", "900", "openai"), ("2", "901", "image_review"), ("3", "902", "embedding"), ("4", "903", "something_new")):
            if goods_no != "1":
                seed_product(goods_no, f"상품 {goods_no}", sizes=(("10", "250", 90000),))
                store.save_kream_quotes(kream_id, [{"size": "250", "instantBuy": 100000, "instantSell": None}])
            store.set_manual_match(goods_no, kream_id, "자동 판별기 근거", method=method, confidence=0.94)
        with mock.patch.dict(os.environ, {"KREAM_TRUST_EXISTING_MATCHES": "1"}):      # even the "trust old matches" switch does not apply
            infos = {g: cmp.match_info(store.match_for(g)) for g in ("1", "2", "3", "4")}
            self.assertEqual({(i["status"], i["kind"], i["usable"]) for i in infos.values()}, {("AI 등록(미검수)", "ai_unreviewed", False)})
            self.assertEqual([self.priced(g) for g in ("1", "2", "3", "4")], [[], [], [], []])
        self.assertEqual(infos["1"]["source"], "언어 모델(자동 판별기)")
        self.assertEqual(infos["2"]["source"], "AI 보조 이미지 검토")
        self.assertEqual(len(store._rows("SELECT 1 FROM kream_search_cache WHERE match_method IN ('openai', 'image_review', 'embedding', 'something_new')")), 4)
        self.assertEqual(store.counts()["matched"], 0)
        queue = {i["goodsNo"]: i for i in vlm_review.queue(["1", "2"])}
        self.assertEqual((queue["1"]["state"], [c["kreamId"] for c in queue["1"]["candidates"]]), ("pending", ["900"]))
        self.assertEqual(vlm_review.metrics(["1", "2", "3", "4"])["aiUnreviewed"], 4)

        with self.search(self.found()) as search:                        # a waiting match is not searched (and billed) again
            ctx = FakeContext()
            pipeline.match_products(store.products(goods_nos=["1"]), ctx)
            search.assert_not_called()
        self.assertEqual(ctx.summary, {"awaitingReview": 1})

        vlm_review.decide("1", "approve", "900")
        approved = cmp.match_info(store.match_for("1"))
        self.assertEqual((approved["status"], approved["usable"], approved["originMethod"], approved["originReason"]),
                         ("검수 승인", True, "openai", "자동 판별기 근거"))               # the source survives the approval
        self.assertEqual(self.priced("1"), [100000])

        vlm_review.decide("2", "reject", "901", "색상이 다름")
        rejected = store.match_for("2")
        self.assertEqual((rejected["kream_product_id"], rejected["match_verified"], rejected["review_state"], rejected["origin_method"]),
                         ("901", 0, "rejected", "image_review"))                       # not deleted
        with mock.patch.dict(os.environ, {"KREAM_TRUST_EXISTING_MATCHES": "1"}):
            self.assertEqual((cmp.match_info(rejected)["status"], cmp.match_info(rejected)["usable"]), ("검수 거절", False))
            self.assertEqual(self.priced("2"), [])
        with self.search(self.found(901)) as search:
            ctx = FakeContext()
            pipeline.match_products(store.products(goods_nos=["2"]), ctx)
            search.assert_not_called()                                   # a rejected match is not proposed again by itself
        self.assertEqual(ctx.summary, {"rejected": 1})

    def test_approval_from_the_match_tab_is_recorded_as_a_review(self):
        store.set_manual_match("1", "900", "자동 판별기 근거", method="openai", confidence=0.9)
        jobs = JobManager(persist=False)
        client = create_app(jobs).test_client()
        token = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/").text)[1]
        row = client.get("/api/matches").get_json()["matches"][0]
        self.assertEqual((row["status"], row["usable"]), ("AI 등록(미검수)", False))
        response = client.post("/api/matches/approve", json={"ids": ["1"]}, headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 200)
        row = client.get("/api/matches").get_json()["matches"][0]
        self.assertEqual((row["status"], row["usable"], row["originMethod"]), ("검수 승인", True, "openai"))
        review = store.reviews(["1"])[-1]
        self.assertEqual((review["action"], review["kreamId"], review["previousMethod"]), ("approve", "900", "openai"))


if __name__ == "__main__":
    unittest.main()

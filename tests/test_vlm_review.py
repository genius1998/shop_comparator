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
        self.assertEqual(cmp.match_info(store.match_for("1"))["status"], "매칭 없음")
        self.assertFalse(any(r["kreamBuy"] for r in cmp.build(["1"])))
        history = store.reviews(["1"])
        self.assertEqual([(r["action"], r["previousKreamId"], r["previousMethod"]) for r in history],
                         [("hold", "500", "openai"), ("reject", "500", "openai")])            # the trail survives the unmatch
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


if __name__ == "__main__":
    unittest.main()

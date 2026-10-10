"""Ground-truth labelling and the evaluation that runs on it (temp folders; no network; no label is invented)."""
import csv
import io
import json
import os
import re
import unittest
from unittest import mock

from helpers import TempDbTestCase, seed_product
from shop_comparator.matching import gold, gold_eval
from shop_comparator.services.jobs import JobManager
from shop_comparator.storage import store
from shop_comparator.web.web_app import create_app


def case(case_id, goods_no, model, candidates, stratum="similar", name="상품"):
    return {"caseId": case_id, "stratum": stratum, "hash": "h" + case_id,
            "musinsa": {"goodsNo": goods_no, "brand": "브랜드", "name": name, "model": model, "options": ["250", "260"],
                        "image": f"{case_id}/musinsa.jpg", "url": f"https://www.musinsa.com/products/{goods_no}"},
            "candidates": [{"kreamId": k, "name": n, "brand": "브랜드", "model": m, "options": o, "image": f"{case_id}/kream_{k}.jpg",
                            "url": f"https://kream.co.kr/products/{k}"} for k, n, m, o in candidates]}


CASES = [
    case("G001", "1", "KJ0774", [("11", "러너 블루", "KJ0775", None), ("10", "러너 그레이", "KJ0774", ["250"])], "code_single", "러너 / KJ0774"),
    case("G002", "2", "SHOP01", [("20", "더비 블랙", "", None)], "similar", "더비 블랙"),
    case("G003", "3", "SHOP02", [("31", "다른 부츠", "", ["250"]), ("30", "부츠", "", ["250"])], "maybe_none", "부츠"),
    case("G004", "4", "SHOP03", [("40", "로퍼", "", None)], "maybe_none", "로퍼"),
]
OUTPUTS = {
    "G001": {"goodsNo": "1", "searchOrder": ["11", "10"], "vlm": {"decision": "same", "kreamId": "10", "model": "gpt-5.4-mini-x", "confidence": 0.99},
             "registered": {"kind": "rule", "method": "model_code", "kreamId": "10"}},
    "G002": {"goodsNo": "2", "searchOrder": ["20"], "vlm": {"decision": "same", "kreamId": "20", "model": "gpt-5.4-mini-x", "confidence": 0.9},
             "registered": {"kind": "ai_unreviewed", "method": "openai", "kreamId": "20"}},
    "G003": {"goodsNo": "3", "searchOrder": ["30", "31"], "vlm": {"decision": "same", "kreamId": "30", "model": "gpt-5.4-mini-x", "confidence": 0.95},
             "registered": {"kind": "none", "method": "", "kreamId": ""}},
    "G004": {"goodsNo": "4", "searchOrder": ["40"], "vlm": None, "registered": {"kind": "none", "method": "", "kreamId": ""}},
}


class GoldCase(TempDbTestCase):
    def setUp(self):
        super().setUp()
        os.makedirs(gold.GOLD_DIR)
        with open(gold.path("cases.json"), "w", encoding="utf-8") as f:
            json.dump({"cases": CASES, "strataCounts": {"code_single": 1, "similar": 1, "maybe_none": 2}, "selectionNote": "선정 기준"}, f, ensure_ascii=False)
        with open(gold.path("method_outputs.json"), "w", encoding="utf-8") as f:
            json.dump(OUTPUTS, f, ensure_ascii=False)

    def human(self):
        return mock.patch.dict(os.environ, {"SHOP_AUTOMATION": "0"})


class LabelStoreTests(GoldCase):
    def test_labelling_screen_gets_no_method_output(self):
        text = json.dumps(gold.queue("reviewer-a"), ensure_ascii=False)
        for hidden in ("vlm", "confidence", "registered", "searchOrder", "stratum", "model_code", "openai", "0.99", "maybe_none", "code_single"):
            self.assertNotIn(hidden, text)
        first = gold.queue()[0]
        self.assertEqual([c["kreamId"] for c in first["candidates"]], ["11", "10"])      # frozen order, not re-sorted by any method
        # sizes exist for one candidate only: showing them would point at the earlier match, so none are shown
        self.assertEqual((first["candidateOptionsShown"], [c["options"] for c in first["candidates"]]), (False, [None, None]))
        third = gold.queue()[2]
        self.assertEqual((third["candidateOptionsShown"], third["candidates"][0]["options"]), (True, ["250"]))
        self.assertEqual(first["musinsa"]["options"], ["250", "260"])

    def test_nothing_counts_as_human_until_a_person_saves_a_label(self):
        self.assertEqual((gold.status()["humanLabelled"], gold.status()["automationRecords"]), (0, 0))
        gold.add_label("G001", "capture-script", "candidate", "10")                 # SHOP_AUTOMATION=1 in tests
        status = gold.status()
        self.assertEqual((status["humanLabelled"], status["automationRecords"], status["humanRemaining"]), (0, 1, 4))
        self.assertEqual(gold_eval.evaluate()["status"], "사람 검수 대기")
        self.assertNotIn("methods", gold_eval.evaluate())                           # no number without a human label
        with self.human():
            gold.add_label("G001", "reviewer-a", "candidate", "10", started="2026-10-10T01:00:00Z", ended="2026-10-10T01:00:20Z",
                           seconds=20.0, hidden=3.5, interruptions=1)
        status = gold.status()
        self.assertEqual((status["humanLabelled"], status["automationRecords"], status["reviewers"]), (1, 1, ["reviewer-a"]))
        row = gold.labels("human_ui")[0]
        self.assertEqual((row["caseId"], row["reviewer"], row["decision"], row["kreamId"], row["snapshotHash"], row["seconds"],
                          row["hiddenSeconds"], row["interruptions"]), ("G001", "reviewer-a", "candidate", "10", "hG001", 20.0, 3.5, 1))

    def test_invalid_labels_are_refused(self):
        with self.human():
            for args in (("G999", "reviewer-a", "none", None, "근거"), ("G001", "", "candidate", "10"), ("G001", "reviewer-a", "maybe", "10"),
                         ("G001", "reviewer-a", "candidate", "999"), ("G001", "reviewer-a", "none", None, ""), ("G001", "reviewer-a", "unsure")):
                with self.assertRaises(ValueError):
                    gold.add_label(*args)
            self.assertEqual(gold.labels(), [])
            gold.add_label("G004", "reviewer-a", "none", "40", "브랜드가 다름", seconds=-5)   # an id next to "none" is dropped, a bad timer too
            row = gold.labels()[0]
            self.assertEqual((row["kreamId"], row["seconds"], row["rationale"]), (None, None, "브랜드가 다름"))

    def test_newest_label_of_a_case_counts_and_history_is_kept(self):
        with self.human():
            gold.add_label("G002", "reviewer-a", "candidate", "20")
            gold.add_label("G002", "reviewer-a", "none", None, "밑창이 다름")
            gold.add_label("G002", "reviewer-b", "candidate", "20")
        self.assertEqual(len(gold.labels()), 3)
        self.assertEqual(gold.latest_labels("human_ui", "reviewer-a")["G002"]["decision"], "none")
        self.assertEqual(gold.latest_labels("human_ui")["G002"]["reviewer"], "reviewer-b")
        with self.human():
            self.assertEqual(gold.queue("reviewer-a")[1]["label"]["decision"], "none")
            self.assertIsNone(gold.queue("reviewer-c")[1]["label"])                 # another reviewer never sees someone else's answer
        self.assertIsNone(gold.queue("reviewer-a")[1]["label"])                     # a script session does not see a person's answers either


class EvaluationTests(GoldCase):
    def label_all(self):
        with self.human():
            gold.add_label("G001", "reviewer-a", "candidate", "10")
            gold.add_label("G002", "reviewer-a", "candidate", "20")
            gold.add_label("G003", "reviewer-a", "none", None, "두 후보 모두 다른 부츠")
            gold.add_label("G004", "reviewer-a", "unsure", None, "이미지가 너무 작음")

    def test_execution_scope_is_reported_without_any_label(self):
        report = gold_eval.evaluate()
        self.assertEqual({k: (v["run"], v["notRun"]) for k, v in report["execution"].items()},
                         {"A0": (4, 0), "A": (4, 0), "B": (4, 0), "C": (3, 1), "D": (3, 1)})     # G004 has no stored verdict
        self.assertEqual(report["notRunMethods"]["B-LLM"]["status"], "미실행")
        self.assertIn("사람 검수 대기", gold_eval.render(report))
        self.assertIn("LLM 아님", gold_eval.NAMES["B"])

    def test_scores_use_human_labels_and_keep_not_run_apart(self):
        self.label_all()
        report = gold_eval.evaluate()
        self.assertEqual((report["humanLabelled"], report["humanUsable"], report["humanUnsure"]), (4, 3, 1))
        a0, a, c = (report["methods"][k] for k in ("A0", "A", "C"))
        # A0: G001 exact code -> right; G002 first result -> right; G003 first result where the person said "none" -> wrong
        self.assertEqual((a0["right_pick"], a0["wrong_pick"], a0["notRun"]), (2, 1, 0))
        self.assertEqual((a0["precision"]["num"], a0["precision"]["den"]), (2, 3))
        self.assertEqual((a0["wrongOnNone"]["num"], a0["wrongOnNone"]["den"]), (1, 1))
        # A: decides only G001 and hands the rest over
        self.assertEqual((a["right_pick"], a["wrong_pick"], a["human"]), (1, 0, 2))
        self.assertEqual((a["foundRate"]["num"], a["foundRate"]["den"], a["deferRate"]["num"], a["deferRate"]["den"]), (1, 2, 2, 3))
        # C: all three labelled cases have a stored verdict; it picked a product on the "none" case
        self.assertEqual((c["run"], c["notRun"], c["right_pick"], c["wrong_pick"]), (3, 0, 2, 1))
        for metric in ("precision", "foundRate", "deferRate", "wrongOnNone", "coverage"):
            self.assertIn("formula", c[metric])
        self.assertEqual(report["commonSet"]["cases"], 3)
        self.assertEqual(report["byStratum"]["maybe_none"]["labelled"], 1)

    def test_a_case_without_a_stored_verdict_is_not_run_not_wrong(self):
        with self.human():
            gold.add_label("G004", "reviewer-a", "candidate", "40")
        report = gold_eval.evaluate()
        c = report["methods"]["C"]
        self.assertEqual((c["labelled"], c["run"], c["notRun"], c["wrong_pick"], c["missed"], c["human"]), (1, 0, 1, 0, 0, 0))
        self.assertIsNone(c["precision"]["value"])
        self.assertEqual(report["commonSet"]["cases"], 0)

    def test_reviewer_filter(self):
        self.label_all()
        with self.human():
            gold.add_label("G003", "reviewer-b", "candidate", "30")
        self.assertEqual(gold_eval.evaluate("reviewer-a")["methods"]["C"]["wrong_pick"], 1)
        self.assertEqual(gold_eval.evaluate("reviewer-b")["methods"]["C"]["right_pick"], 1)


class GoldApiTests(GoldCase):
    def setUp(self):
        super().setUp()
        self.client = create_app(JobManager(persist=False)).test_client()
        self.token = re.search(r'name="csrf-token" content="([^"]+)"', self.client.get("/").text)[1]
        os.makedirs(gold.path("images", "G001"))
        open(gold.path("images", "G001", "musinsa.jpg"), "wb").close()

    def post(self, path, body):
        return self.client.post(path, json=body, headers={"X-CSRF-Token": self.token})

    def test_labelling_api_hides_ai_output_and_separates_automation(self):
        listing = self.client.get("/api/gold?reviewer=reviewer-a")
        self.assertEqual(len(listing.get_json()["items"]), 4)
        for hidden in ("confidence", "searchOrder", "registered", "stratum", "gpt-5.4"):
            self.assertNotIn(hidden, listing.text)
        image = self.client.get("/api/gold/image/G001/musinsa.jpg")
        self.assertEqual(image.status_code, 200)
        image.close()
        for bad in ("/api/gold/image/G001/..%2flabels.sqlite", "/api/gold/image/X001/musinsa.jpg", "/api/gold/image/G001/cases.json"):
            self.assertEqual(self.client.get(bad).status_code, 404)
        self.assertEqual(self.post("/api/gold/G001", {"reviewer": "reviewer-a", "decision": "candidate", "kreamId": "999"}).status_code, 400)
        saved = self.post("/api/gold/G001", {"reviewer": "capture", "decision": "candidate", "kreamId": "10", "seconds": 4.2, "hiddenSeconds": 0,
                                             "interruptions": 0, "startedAt": "2026-10-10T00:00:00Z", "endedAt": "2026-10-10T00:00:04Z"})
        self.assertEqual((saved.status_code, saved.get_json()["origin"], saved.get_json()["status"]["humanLabelled"]), (200, "automation", 0))
        report = self.client.get("/api/gold/report").get_json()
        self.assertEqual((report["report"]["status"], report["report"]["automationRecords"]), ("사람 검수 대기", 1))
        self.assertEqual((report["timing"][0]["context"], report["timing"][0]["mode"], report["timing"][0]["origin"]), ("gold", "blind", "automation"))
        with mock.patch.dict(os.environ, {"SHOP_AUTOMATION": "0"}):
            self.assertEqual(self.post("/api/gold/G001", {"reviewer": "reviewer-a", "decision": "candidate", "kreamId": "10"}).get_json()["origin"], "human_ui")
        rows = list(csv.DictReader(io.StringIO(self.client.get("/api/export/gold-labels.csv").data.decode("utf-8-sig"))))
        self.assertEqual([(r["origin"], r["reviewer"]) for r in rows], [("automation", "capture"), ("human_ui", "reviewer-a")])

    def test_operational_review_timing_is_recorded_with_interruptions(self):
        seed_product("1", "러너")
        store.set_manual_match("1", "900", "근거", method="openai", confidence=0.9)
        response = self.post("/api/review/1", {"action": "hold", "mode": "manual", "seconds": 31.5, "hiddenSeconds": 12.0, "interruptions": 2,
                                               "startedAt": "2026-10-10T00:00:00Z", "endedAt": "2026-10-10T00:00:31Z", "reviewer": "reviewer-a"})
        self.assertEqual(response.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(self.client.get("/api/export/review-timing.csv").data.decode("utf-8-sig"))))
        self.assertEqual((rows[0]["context"], rows[0]["caseId"], rows[0]["mode"], rows[0]["decision"], rows[0]["seconds"], rows[0]["hiddenSeconds"],
                          rows[0]["interruptions"], rows[0]["origin"]), ("ops", "1", "manual", "hold", "31.5", "12.0", "2", "automation"))


if __name__ == "__main__":
    unittest.main()

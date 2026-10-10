"""Matching evaluation and the local vision-model backend (mock data, no network)."""
import json
import unittest
from unittest import mock

from shop_comparator.matching import evaluate, vlm_match
from helpers import TempDbTestCase


def product(goods_no, serial, name, candidates, vlm=None, review=None):
    row = {"goodsNo": goods_no, "brand": "브랜드", "name": name, "serial": serial,
           "candidates": [{"kreamId": k, "name": "후보", "model": m} for k, m in candidates], "vlm": vlm or {}}
    if review:
        row["review"] = review
    return row


class LegacyRuleTests(unittest.TestCase):
    def test_identical_model_code_wins_over_position(self):
        entry = product("1", "CW2288-001", "에어포스", [("10", "DD8959-100"), ("20", "315122-001/CW2288-001")])
        self.assertEqual(evaluate.legacy_pick(entry), ("20", "exact"))

    def test_without_identical_code_the_first_result_is_taken(self):
        entry = product("1", "NBPDGF733B_19", "NBPDGF733B / U740BM2 (BLACK)", [("10", "M1906REH"), ("20", "U740BM2")])
        self.assertEqual(evaluate.legacy_pick(entry), ("10", "first"))
        # the current rule reads the code written in the product name instead
        self.assertEqual(evaluate.model_code_pick(entry), ("20", "model_code"))

    def test_shared_model_code_is_not_decided_by_the_current_rule(self):
        entry = product("1", "CW2288-111", "에어포스 화이트", [("10", "CW2288-111"), ("20", "315122-111/CW2288-111")])
        self.assertEqual(evaluate.legacy_pick(entry)[1], "exact")
        self.assertEqual(evaluate.model_code_pick(entry), ("", "ambiguous"))


class JudgeTests(unittest.TestCase):
    def test_labels(self):
        coded = product("1", "KJ0774", "아디제로", [("10", "KJ0774"), ("20", "")])
        self.assertTrue(evaluate.judge(coded, "10"))
        self.assertFalse(evaluate.judge(coded, "20"))
        same = product("2", "RM0072", "로퍼", [("10", ""), ("20", "")], review={"label": "same", "kreamId": "20"})
        self.assertTrue(evaluate.judge(same, "20"))
        self.assertFalse(evaluate.judge(same, "10"))
        none = product("3", "RM0072", "로퍼", [("10", "")], review={"label": "none", "kreamId": ""})
        self.assertFalse(evaluate.judge(none, "10"))
        rejected = product("4", "RM0072", "로퍼", [("10", ""), ("20", "")], review={"label": "not", "kreamId": "10"})
        self.assertFalse(evaluate.judge(rejected, "10"))
        self.assertIsNone(evaluate.judge(rejected, "20"))          # the other candidate was never reviewed
        self.assertIsNone(evaluate.judge(product("5", "RM0072", "로퍼", [("10", "")]), "10"))


class ReportTests(unittest.TestCase):
    def test_counts(self):
        dataset = {"vlm_models": ["m"], "products": {
            "1": product("1", "KJ0774", "아디제로 / KJ0774", [("10", "KJ0774")], vlm={"m": {"decision": "same", "kreamId": "10", "seconds": 2}}),
            "2": product("2", "SHOP01", "러너 / U740BM2", [("10", "M1906R"), ("20", "U740BM2")], vlm={"m": {"decision": "same", "kreamId": "10", "seconds": 4}}),
            "3": product("3", "SHOP02", "더비", [("30", ""), ("31", "")], vlm={"m": {"decision": "same", "kreamId": "31", "seconds": 3}},
                         review={"label": "same", "kreamId": "31"}),
            "4": product("4", "SHOP03", "부츠", [("40", "")], vlm={"m": {"decision": "same", "kreamId": "40", "seconds": 3}},
                         review={"label": "not", "kreamId": "40"}),
            "5": product("5", "SHOP04", "로퍼", [("50", "")], vlm={"m": {"decision": "different", "kreamId": "", "seconds": 3}}),
        }}
        report = evaluate.evaluate(dataset)
        self.assertEqual(report["legacy"]["exact"], 1)
        self.assertEqual(report["legacy"]["first"], 4)
        self.assertEqual((report["legacy"]["first_right"], report["legacy"]["first_wrong"], report["legacy"]["first_unreviewed"]), (0, 3, 1))
        self.assertEqual(report["model_code"], {"model_code": 2, "ambiguous": 0, "no_code": 3})
        vlm = report["vlm"]["m"]
        self.assertEqual((vlm["code_checked"], vlm["code_agree"], vlm["code_other"]), (2, 1, 1))
        self.assertEqual((vlm["open_same"], vlm["open_same_right"], vlm["open_same_wrong"], vlm["open_different"]), (2, 1, 1, 1))
        self.assertEqual(vlm["seconds_each"], 3.0)
        self.assertEqual(report["found"], {"legacy_exact": 1, "model_code": 2, "model_code_plus_reviewed": 3})
        self.assertIn("기존 규칙", evaluate.render(report))


class PriceHighlightTests(unittest.TestCase):
    def test_highlight_needs_a_higher_kream_price_and_enough_sales(self):
        from shop_comparator.matching.match_review import price_level
        self.assertEqual(price_level(100000, 105000, 10), 1)     # KREAM dearer, sells
        self.assertEqual(price_level(100000, 110000, 10), 2)     # 110% or more
        self.assertEqual(price_level(100000, 120000, 9), 0)      # too few sales
        self.assertEqual(price_level(100000, 100000, 50), 0)     # not dearer
        self.assertEqual(price_level(100000, None, 50), 0)       # no instant-buy price is never a guess
        self.assertEqual(price_level(100000, 120000, None), 0)   # sales not collected

    def test_product_summary_uses_only_sizes_priced_on_both_sides(self):
        from shop_comparator.matching.match_review import price_summary
        rows = [{"size": "250", "musinsaPrice": 100000, "kreamBuy": 95000},
                {"size": "260", "musinsaPrice": 100000, "kreamBuy": 112000},
                {"size": "270", "musinsaPrice": 100000, "kreamBuy": None},
                {"size": "280", "musinsaPrice": None, "kreamBuy": 150000}]
        summary = price_summary(rows, monthly=30)
        self.assertEqual((summary["musinsa"], summary["kream"]), (100000, "95,000~112,000"))
        self.assertEqual((summary["size"], summary["difference"], summary["dearer"], summary["compared"], summary["level"]),
                         ("260", 12000, 1, 2, 2))
        self.assertAlmostEqual(summary["ratio"], 1.12)
        self.assertEqual(price_summary(rows, monthly=3)["level"], 0)
        self.assertEqual(price_summary([], monthly=30)["level"], 0)


class LocalVisionModelTests(TempDbTestCase):
    ENTRY = {"goodsNo": "1", "brand": "나이키", "name": "에어포스", "serial": "CW2288-111", "musinsaImage": "images/1/musinsa.jpg",
             "candidates": [{"kreamId": "12831", "brand": "Nike", "name": "에어포스 1", "image": "images/1/kream_12831.jpg", "model": "CW2288-111"}]}

    def _answer(self, content, status=200):
        response = mock.Mock(status_code=status, text="err")
        response.json.return_value = {"model": "qwen3-vl:8b", "message": {"content": content}, "prompt_eval_count": 900, "eval_count": 60}
        return response

    def test_local_model_gets_the_same_question_and_schema(self):
        answer = json.dumps({"decision": "same", "kream_id": 12831, "confidence": 0.9, "reason": "형태 일치"})
        with mock.patch.object(vlm_match, "_b64", side_effect=lambda path: "B64:" + path), \
                mock.patch.object(vlm_match.requests, "post", return_value=self._answer(answer)) as post:
            result, usage = vlm_match.judge_local(self.ENTRY)
        self.assertEqual(result, {"decision": "same", "kream_id": "12831", "confidence": 0.9, "reason": "형태 일치"})
        self.assertEqual(usage["images"], 2)
        url, payload = post.call_args.args[0], post.call_args.kwargs["json"]
        self.assertTrue(url.startswith("http://127.0.0.1:11434/"))         # stays on this PC
        self.assertEqual(payload["format"], vlm_match.SCHEMA)
        self.assertEqual(payload["messages"][0]["content"], vlm_match.SYSTEM)
        self.assertEqual(payload["messages"][1]["images"], ["B64:images/1/musinsa.jpg", "B64:images/1/kream_12831.jpg"])
        self.assertEqual(json.loads(payload["messages"][1]["content"])["image_order"], ["Musinsa product", "KREAM candidate 12831"])

    def test_malformed_local_answer_is_an_error_not_a_match(self):
        with mock.patch.object(vlm_match, "_b64", return_value="x"), \
                mock.patch.object(vlm_match.requests, "post", return_value=self._answer('{"decision": "maybe"}')):
            with self.assertRaises(RuntimeError):
                vlm_match.judge_local(self.ENTRY)


if __name__ == "__main__":
    unittest.main()

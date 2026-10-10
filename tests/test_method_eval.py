"""Method comparison (A0, A, B, C, D) on a small hand-made dataset; no network."""
import unittest

from shop_comparator.matching import method_eval as me


def product(goods_no, serial, name, candidates, vlm=None, review=None, brand="브랜드"):
    row = {"goodsNo": goods_no, "brand": brand, "name": name, "serial": serial,
           "candidates": [{"kreamId": k, "name": n, "model": m} for k, n, m in candidates],
           "vlm": {"m": vlm} if vlm else {}}
    if review:
        row["review"] = review
    return row


DATASET = {"products": {
    # unique model code; another colourway of the same shoe is also in the search result
    "1": product("1", "KJ0774", "러너 / KJ0774", [("11", "러너 블루", "KJ0775"), ("10", "러너 그레이", "KJ0774")],
                 vlm={"decision": "same", "kreamId": "10"}),
    # shop code on Musinsa, AI-reviewed as the same product
    "2": product("2", "SHOP01", "더비 블랙", [("20", "더비 블랙", "")], vlm={"decision": "same", "kreamId": "20"},
                 review={"label": "same", "kreamId": "20"}),
    # the vision model picked a product the reviewer rejected
    "3": product("3", "SHOP02", "부츠", [("30", "다른 부츠", "")], vlm={"decision": "same", "kreamId": "30"},
                 review={"label": "not", "kreamId": "30"}),
    # nobody checked this one: it must not be scored
    "4": product("4", "SHOP03", "로퍼", [("40", "로퍼", "")], vlm={"decision": "different", "kreamId": ""}),
}}


class CaseTests(unittest.TestCase):
    def test_cases_carry_their_label_source_and_unlabelled_products_are_left_out(self):
        cases = me.cases(DATASET)
        self.assertEqual(sorted((c["goodsNo"], c["kind"], c["source"], c["truth"]) for c in cases), [
            ("1", "answer", "model_code", "10"),
            ("1", "none", "model_code_removed", ""),        # same product with the true candidate removed
            ("2", "answer", "ai_review", "20"),
            ("3", "rejected", "ai_review", "30"),
        ])
        removed = next(c for c in cases if c["source"] == "model_code_removed")
        self.assertEqual([c["kreamId"] for c in removed["entry"]["candidates"]], ["11"])
        self.assertFalse(removed["stored_vlm"])               # no verdict exists for the changed list

    def test_outcomes(self):
        cases = {(c["goodsNo"], c["kind"]): c for c in me.cases(DATASET)}
        answer, none, rejected = cases[("1", "answer")], cases[("1", "none")], cases[("3", "rejected")]
        self.assertEqual(me.outcome(answer, ("pick", "10")), "right_pick")
        self.assertEqual(me.outcome(answer, ("pick", "11")), "wrong_pick")
        self.assertEqual(me.outcome(answer, ("none", "")), "missed")
        self.assertEqual(me.outcome(answer, ("human", "")), "human")
        self.assertEqual(me.outcome(none, ("pick", "11")), "wrong_pick")
        self.assertEqual(me.outcome(none, ("none", "")), "right_none")
        self.assertEqual(me.outcome(rejected, ("pick", "30")), "wrong_pick")
        self.assertEqual(me.outcome(rejected, ("none", "")), "unknown")       # other candidates were never reviewed
        self.assertEqual(me.outcome(none, ("unavailable", "")), "unavailable")


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.report = me.evaluate_all(DATASET, {"dev_ids": ["2"]}, {"1": {"seconds": 2.0, "input_tokens": 1000, "output_tokens": 100,
                                                                        "model": "gpt-5.4-mini-x"},
                                                                  "2": {"seconds": 4.0, "input_tokens": 1000, "output_tokens": 100,
                                                                        "model": "gpt-5.4-mini-x"}})

    def test_original_rule_takes_the_first_result_when_nothing_matches(self):
        a0 = self.report["methods"]["A0"]["all"]
        # 1: exact code -> right.  1 (removed): first result -> wrong.  2: first result happens to be right.  3: first result is the rejected one.
        self.assertEqual((a0["right_pick"], a0["wrong_pick"], a0["human"]), (2, 2, 0))
        self.assertEqual((a0["precision"]["num"], a0["precision"]["den"]), (2, 4))
        self.assertEqual((a0["false_match_on_none"]["num"], a0["false_match_on_none"]["den"]), (1, 1))

    def test_model_code_rule_hands_over_what_it_cannot_decide(self):
        a = self.report["methods"]["A"]["all"]
        self.assertEqual((a["right_pick"], a["wrong_pick"], a["human"]), (1, 0, 3))
        self.assertEqual((a["to_human"]["num"], a["to_human"]["den"]), (3, 4))
        self.assertEqual((a["recall"]["num"], a["recall"]["den"]), (1, 2))

    def test_vision_results_are_not_invented_for_cases_that_were_never_run(self):
        c, d = self.report["methods"]["C"]["all"], self.report["methods"]["D"]["all"]
        self.assertEqual((c["evaluated"], c["unavailable"], c["right_pick"], c["wrong_pick"]), (3, 1, 2, 1))
        self.assertEqual((d["right_pick"], d["wrong_pick"], d["unavailable"]), (2, 1, 1))
        self.assertEqual(self.report["methods"]["B-LLM"]["status"], "미실행")

    def test_split_keeps_variants_of_development_products_out_of_the_evaluation_part(self):
        self.assertEqual((self.report["split"]["dev"], self.report["split"]["eval"]), (1, 3))
        again = me.evaluate_all(DATASET, {"dev_ids": ["1"]}, {})
        self.assertEqual(again["split"]["dev"], 2)             # the constructed "none" case of product 1 goes with it
        self.assertEqual(again["methods"]["A"]["eval"]["cases"], 2)

    def test_time_and_cost_come_from_recorded_calls(self):
        usage = self.report["vlm_usage"]
        self.assertEqual((usage["calls"], usage["seconds_median"], usage["input_tokens"], usage["output_tokens"]), (2, 3.0, 2000, 200))
        self.assertAlmostEqual(usage["cost_usd"], round((2000 * 0.75 + 200 * 4.5) / 1_000_000, 4))
        self.assertIsNone(me.timing({}))
        self.assertIn("미실행", me.render(self.report))


if __name__ == "__main__":
    unittest.main()

"""Vision-model benchmark: pair selection, scoring and the remote backend (mock data, no network)."""
import json
import os
import unittest
from unittest import mock

from shop_comparator.matching import vlm_benchmark as bench


def product(goods_no, serial, candidates, review=None):
    row = {"goodsNo": goods_no, "brand": "브랜드", "name": "상품", "serial": serial,
           "candidates": [{"kreamId": k, "name": "후보", "model": m} for k, m in candidates], "vlm": {}}
    if review:
        row["review"] = review
    return row


class PairSelectionTests(unittest.TestCase):
    DATASET = {"products": {
        "1": product("1", "KJ0774", [("10", "KJ0774"), ("11", "JH6208"), ("12", ""), ("13", "KJ0774/ALT001")]),
        "2": product("2", "SHOP01", [("20", ""), ("21", "")], review={"label": "same", "kreamId": "20"}),
        "3": product("3", "SHOP02", [("30", "")], review={"label": "not", "kreamId": "30"}),
        "4": product("4", "SHOP03", [("40", "")], review={"label": "doubtful", "kreamId": "40"}),
    }}

    def test_labels_come_from_model_codes_and_reviews_only(self):
        dataset = {"products": {k: v for k, v in self.DATASET["products"].items() if k != "1"}}
        dataset["products"]["1"] = product("1", "KJ0774", [("10", "KJ0774"), ("11", "JH6208"), ("12", "")])
        pairs = bench.build_pairs(dataset, lambda goods_no, kream_id: True)
        got = {(p["goodsNo"], p["kreamId"]): (p["label"], p["kind"]) for p in pairs}
        self.assertEqual(got, {
            ("1", "10"): ("same", "code_same"),
            ("1", "11"): ("different", "other_model"),   # different model code -> a different product
            ("2", "20"): ("same", "reviewed_same"),
            ("3", "30"): ("different", "reviewed_not"),
        })                                              # no code ("12") and "doubtful" are never used as labels

    def test_pairs_without_saved_images_are_left_out(self):
        pairs = bench.build_pairs(self.DATASET, lambda goods_no, kream_id: goods_no != "2")
        self.assertNotIn("2", {p["goodsNo"] for p in pairs})

    def test_same_seed_gives_the_same_pairs(self):
        have = lambda goods_no, kream_id: True
        self.assertEqual(bench.build_pairs(self.DATASET, have), bench.build_pairs(self.DATASET, have))


class ScoreTests(unittest.TestCase):
    PAIRS = [{"goodsNo": "1", "kreamId": "10", "label": "same", "kind": "code_same"},
             {"goodsNo": "2", "kreamId": "20", "label": "same", "kind": "reviewed_same"},
             {"goodsNo": "3", "kreamId": "30", "label": "different", "kind": "other_model"},
             {"goodsNo": "4", "kreamId": "40", "label": "different", "kind": "other_model"},
             {"goodsNo": "5", "kreamId": "50", "label": "same", "kind": "code_same"}]

    def test_uncertain_is_not_counted_as_correct(self):
        verdicts = {"1:10": {"result": "same", "confidence": 0.9, "seconds": 2, "input_tokens": 1000, "output_tokens": 100},
                    "2:20": {"result": "uncertain", "confidence": 0.5, "seconds": 3, "input_tokens": 1000, "output_tokens": 100},
                    "3:30": {"result": "same", "confidence": 0.95, "seconds": 4, "input_tokens": 1000, "output_tokens": 100},
                    "4:40": {"result": "different", "confidence": 0.9, "seconds": 5, "input_tokens": 1000, "output_tokens": 100}}
        s = bench.score(self.PAIRS, verdicts)
        self.assertEqual((s["pairs"], s["correct"], s["uncertain"]), (4, 2, 1))      # pair 5 has no verdict yet
        self.assertEqual((s["tp"], s["fp"], s["fn"], s["tn"]), (1, 1, 1, 1))
        self.assertEqual((s["precision"], s["recall"], s["false_same_rate"]), (0.5, 0.5, 0.5))
        self.assertEqual(s["by_kind"]["other_model"], {"pairs": 2, "correct": 1, "uncertain": 0})
        self.assertEqual(s["wrong_same_confidence"], [0.95])
        self.assertAlmostEqual(bench.cost_krw("gpt-5.4-mini-2026-03-17", s),
                               (4000 * 0.75 + 400 * 4.50) / 1_000_000 * bench.KRW_PER_USD / 4)
        self.assertIsNone(bench.cost_krw("qwen3-vl-8b", s))                          # self-hosted: no list price


class RemoteBackendTests(unittest.TestCase):
    def _run(self, stdout, code=0):
        return mock.Mock(returncode=code, stdout=stdout.encode("utf-8"))

    def test_remote_answer_is_normalised(self):
        out = json.dumps({"result": "SAME", "confidence": 0.95, "reason": "형태 일치", "latency_ms": 2100})
        with mock.patch.dict(os.environ, {"VLM_REMOTE_COMMAND": r"C:\tools\compare.bat", "VLM_REMOTE_MODEL": "qwen"}), \
                mock.patch.object(bench.subprocess, "run", return_value=self._run(out)) as run:
            verdict = bench.ask_remote(r"C:\a.jpg", r"C:\b.jpg")
        self.assertEqual((verdict["result"], verdict["confidence"], verdict["model"]), ("same", 0.95, "qwen"))
        self.assertEqual(run.call_args.args[0], ["cmd", "/c", r"C:\tools\compare.bat", "--image-a", r"C:\a.jpg", "--image-b", r"C:\b.jpg"])

    def test_same_model_in_another_colour_counts_as_different(self):
        out = json.dumps({"result": "SAME_MODEL_DIFFERENT_VARIANT", "confidence": 0.95, "reason": "colorway differs"})
        with mock.patch.dict(os.environ, {"VLM_REMOTE_COMMAND": "compare"}), \
                mock.patch.object(bench.subprocess, "run", return_value=self._run(out)):
            verdict = bench.ask_remote("a", "b")
        self.assertEqual((verdict["result"], verdict["raw_result"]), ("different", "same_model_different_variant"))

    def test_remote_failure_is_an_error_not_a_verdict(self):
        with mock.patch.dict(os.environ, {"VLM_REMOTE_COMMAND": "compare"}):
            with mock.patch.object(bench.subprocess, "run", return_value=self._run("error: tunnel did not open", code=3)):
                with self.assertRaises(RuntimeError):
                    bench.ask_remote("a", "b")
            with mock.patch.object(bench.subprocess, "run", return_value=self._run('{"result": "MAYBE"}')):
                with self.assertRaises(RuntimeError):
                    bench.ask_remote("a", "b")

    def test_run_stops_after_two_failures_in_a_row(self):
        pairs = [{"goodsNo": str(i), "kreamId": "1", "label": "same", "kind": "code_same"} for i in range(5)]
        saved = {}
        with mock.patch.object(bench, "_load", side_effect=lambda path, default: pairs if path == bench.PAIRS else {}), \
                mock.patch.object(bench, "_save", side_effect=lambda path, value: saved.update(value)), \
                mock.patch.object(bench, "image_paths", return_value=("a", "b")), \
                mock.patch("builtins.print"), \
                mock.patch.object(bench, "ask_remote", side_effect=RuntimeError("tunnel did not open")) as ask:
            bench.run("remote")
        self.assertEqual(ask.call_count, 2)          # no hammering of someone else's server
        self.assertEqual(saved, {})


if __name__ == "__main__":
    unittest.main()

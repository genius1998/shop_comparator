# -*- coding: utf-8 -*-

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shop_comparator.matching import product_matcher
from shop_comparator.matching.product_matcher import DEFAULT_OPENAI_MATCH_MODEL, cosine_similarity, embedding_match_decision, extract_style_codes, load_local_env, local_match_decision, openai_match_decision


class ProductMatcherTests(unittest.TestCase):
    def test_default_openai_model_is_low_cost_nano(self):
        self.assertEqual(DEFAULT_OPENAI_MATCH_MODEL, "gpt-5.4-nano")

    def test_load_local_env_can_preserve_existing_env_when_requested(self):
        key = "PRODUCT_MATCHER_TEST_VALUE"
        old_value = os.environ.get(key)
        old_loaded = product_matcher._ENV_LOADED
        os.environ[key] = "already-set"

        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                env_file = Path(tmp_dir) / ".env"
                env_file.write_text(f"{key}=from-file\nNEW_MATCH_ENV=ok\n", encoding="utf-8")
                product_matcher._ENV_LOADED = False
                load_local_env(env_file, override=False)

            self.assertEqual(os.environ[key], "already-set")
            self.assertEqual(os.environ["NEW_MATCH_ENV"], "ok")
        finally:
            product_matcher._ENV_LOADED = old_loaded
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value
            os.environ.pop("NEW_MATCH_ENV", None)

    def test_load_local_env_prefers_file_values_by_default(self):
        key = "PRODUCT_MATCHER_OVERRIDE_TEST_VALUE"
        old_value = os.environ.get(key)
        old_loaded = product_matcher._ENV_LOADED
        os.environ[key] = "already-set"

        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                env_file = Path(tmp_dir) / ".env"
                env_file.write_text(f"{key}=from-file\n", encoding="utf-8")
                product_matcher._ENV_LOADED = False
                load_local_env(env_file)

            self.assertEqual(os.environ[key], "from-file")
        finally:
            product_matcher._ENV_LOADED = old_loaded
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value

    @mock.patch("shop_comparator.matching.product_matcher.load_local_env")
    @mock.patch("shop_comparator.matching.product_matcher.requests.post")
    def test_openai_no_match_does_not_return_candidate_as_product(self, post, _load_env):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "output_text": (
                        '{"decision":"no_match","matched_product_id":"123",'
                        '"confidence":0.94,"reason":"Different brand and model code."}'
                    )
                }

        old_env = {
            key: os.environ.get(key)
            for key in ("OPENAI_MATCHING_ENABLED", "OPENAI_API_KEY", "OPENAI_MATCH_MODEL")
        }
        os.environ["OPENAI_MATCHING_ENABLED"] = "1"
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["OPENAI_MATCH_MODEL"] = DEFAULT_OPENAI_MATCH_MODEL
        post.return_value = FakeResponse()

        try:
            decision = openai_match_decision(
                {"name": "A", "brand": "Brand A", "serial_number": "AAA111"},
                [{"id": 123, "brand": "Brand B", "name_ko": "B", "all_text": "BBB222"}],
            )
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertFalse(decision.matched)
        self.assertIsNone(decision.product)
        self.assertEqual(decision.confidence, 0.0)
        self.assertIn("decision=no_match", decision.reason)

    def test_cosine_similarity(self):
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0)

    def test_extract_style_codes_ignores_hyphenated_product_words_without_digits(self):
        self.assertEqual(extract_style_codes("Classic Slip-On Checkerboard Black White"), [])

    @mock.patch("shop_comparator.matching.product_matcher.load_local_env")
    @mock.patch("shop_comparator.matching.product_matcher.embedding_for_text")
    def test_embedding_match_accepts_clear_vector_candidate(self, embed, _load_env):
        old_env = {
            key: os.environ.get(key)
            for key in (
                "OPENAI_EMBEDDING_MATCHING_ENABLED",
                "OPENAI_EMBEDDING_ACCEPT_THRESHOLD",
                "OPENAI_EMBEDDING_MARGIN_THRESHOLD",
            )
        }
        os.environ["OPENAI_EMBEDDING_MATCHING_ENABLED"] = "1"
        os.environ["OPENAI_EMBEDDING_ACCEPT_THRESHOLD"] = "0.8"
        os.environ["OPENAI_EMBEDDING_MARGIN_THRESHOLD"] = "0.1"

        def fake_embedding(text):
            if "Classic Slip-On Checkerboard Black White" in text and "Vans Classic" not in text:
                return [1.0, 0.0]
            if "Vans Classic" in text:
                return [0.98, 0.02]
            return [0.0, 1.0]

        embed.side_effect = fake_embedding

        try:
            decision = embedding_match_decision(
                {"name": "Classic Slip-On Checkerboard Black White", "brand": "반스", "serial_number": ""},
                [
                    {
                        "id": 1,
                        "brand": "VANS",
                        "name_ko": "Classic Slip-On Checkerboard Black White",
                        "all_text": "Vans Classic Slip-On Checkerboard Black White",
                    },
                    {
                        "id": 2,
                        "brand": "VANS",
                        "name_ko": "Old Skool Black White",
                        "all_text": "Vans Old Skool Black White",
                    },
                ],
            )
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertTrue(decision.matched)
        self.assertEqual(decision.product["id"], 1)
        self.assertEqual(decision.method, "embedding")

    def test_rejects_first_result_with_different_style_and_brand(self):
        decision = local_match_decision(
            {
                "name": "Formica core sneakers(Mesh black)_OK1DX26052MBK",
                "brand": "아카이브앱크",
                "serial_number": "OK1DX26052MBK",
            },
            [
                {
                    "id": 798932,
                    "brand": "NEGATIVE THREE",
                    "name_ko": "네거티브쓰리 커브드 레이스 No.2 스니커즈 블랙",
                    "all_text": "NegativeThree Curved Lace No.2 Sneakers Black",
                }
            ],
        )

        self.assertFalse(decision.matched)

    def test_accepts_exact_style_code_match(self):
        decision = local_match_decision(
            {
                "name": "XA 프로 3D_L41617400",
                "brand": "살로몬",
                "serial_number": "L41617400",
            },
            [
                {
                    "id": 60333,
                    "brand": "SALOMON",
                    "name_ko": "살로몬 XA 프로 3D 블랙 - L41617400",
                    "all_text": "Salomon XA Pro 3D Black L41617400",
                }
            ],
        )

        self.assertTrue(decision.matched)
        self.assertEqual(decision.product["id"], 60333)

    def test_rejects_different_collaboration_with_different_style_code(self):
        decision = local_match_decision(
            {
                "name": "로미타 플랫폼 벨크로 메리제인 FLOTEA1W05",
                "brand": "오찌",
                "serial_number": "OZSQGA104B",
            },
            [
                {
                    "id": 532312,
                    "brand": "SALOMON",
                    "name_ko": "살로몬 x 꼼데가르송 RX 마리 제인 플랫폼 화이트 - L47960400",
                    "all_text": "Salomon x Comme des Garcons RX Marie Jeanne Platform White L47960400",
                }
            ],
        )

        self.assertFalse(decision.matched)

    def test_accepts_high_confidence_name_match_without_style_code(self):
        decision = local_match_decision(
            {
                "name": "Classic Slip-On Checkerboard Black White",
                "brand": "반스",
                "serial_number": "",
            },
            [
                {
                    "id": 12345,
                    "brand": "VANS",
                    "name_ko": "Classic Slip-On Checkerboard Black White",
                    "all_text": "Vans Classic Slip-On Checkerboard Black White",
                }
            ],
        )

        self.assertTrue(decision.matched)


if __name__ == "__main__":
    unittest.main()

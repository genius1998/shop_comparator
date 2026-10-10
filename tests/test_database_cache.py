# -*- coding: utf-8 -*-

import tempfile
import unittest
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from shop_comparator.storage import database


class FakeKreamId(str):
    pass


class DatabaseCacheTests(unittest.TestCase):
    def test_kream_search_cache_preserves_match_metadata(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test.sqlite"

            @contextmanager
            def fake_db_conn():
                conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                try:
                    yield conn
                    conn.commit()
                finally:
                    conn.close()

            product_id = FakeKreamId("12345")
            product_id.match_verified = True
            product_id.match_method = "openai"
            product_id.match_confidence = 0.91
            product_id.match_reason = "same model and color"
            product_id.kream_brand = "Brand"
            product_id.kream_product_name = "Product Name"
            product_id.kream_image_url = "https://example.com/image.jpg"
            product_id.kream_all_text = "Brand Product Name"

            with mock.patch("shop_comparator.storage.database.get_db_conn", side_effect=fake_db_conn):
                database.init_db()
                database.cache_kream_search("goods-1", product_id, "https://kream.co.kr/products/12345")
                row = database.get_cached_kream_search("goods-1")

        self.assertEqual(row["kream_product_id"], "12345")
        self.assertEqual(row["match_verified"], 1)
        self.assertEqual(row["match_method"], "openai")
        self.assertEqual(row["match_confidence"], 0.91)
        self.assertEqual(row["match_reason"], "same model and color")
        self.assertEqual(row["kream_brand"], "Brand")
        self.assertEqual(row["kream_product_name"], "Product Name")
        self.assertEqual(row["kream_image_url"], "https://example.com/image.jpg")
        self.assertEqual(row["kream_all_text"], "Brand Product Name")


if __name__ == "__main__":
    unittest.main()

"""Shared test helpers. Everything here uses mock data; no shop is contacted."""
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from shop_comparator.storage import database
from shop_comparator.storage import store
from shop_comparator.services.jobs import JobContext

FIXTURES = Path(__file__).parent / "fixtures"

# Tests must not write into the real logs/app.jsonl.
import logging
from shop_comparator.core import app_logging
_null = logging.getLogger("shop_comparator_tests")
_null.addHandler(logging.NullHandler())
_null.propagate = False
app_logging._logger = _null


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class TempDbTestCase(unittest.TestCase):
    """Runs each test against a throw-away SQLite file, never the user's DB."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "test.sqlite"
        opened = []

        def connect():
            conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            opened.append(conn)
            return conn

        def close_all():  # Windows cannot delete an open SQLite file
            for conn in opened:
                conn.close()

        patcher = mock.patch.object(database, "get_db_conn", side_effect=connect)
        patcher.start()
        # Never let a test reach a real shop login or read the user's .env accounts.
        import os
        from shop_comparator.shops import site_login
        clean = {k: v for k, v in os.environ.items()
                 if k not in {"MUSINSA_ID", "MUSINSA_PASSWORD", "KREAM_ID", "KREAM_PASSWORD", "POIZON_ID", "POIZON_PASSWORD", "KREAM_BEARER"}}
        guards = [mock.patch.dict(os.environ, clean, clear=True),
                  mock.patch("shop_comparator.matching.product_matcher.load_local_env"),
                  mock.patch.object(site_login, "login_musinsa", side_effect=AssertionError("real login blocked in tests")),
                  mock.patch.object(site_login, "login_kream", side_effect=AssertionError("real login blocked in tests")),
                  mock.patch("shop_comparator.shops.browser_transport.new_driver", side_effect=AssertionError("real browser blocked in tests"))]
        for guard in guards:
            guard.start()
            self.addCleanup(guard.stop)
        # Never let a test touch the user's search-exclusion CSV.
        from shop_comparator.services import pipeline
        blacklist = mock.patch.object(pipeline, "_blacklist_path", return_value=str(Path(self._tmp.name) / "blacklist.csv"))
        blacklist.start()
        self.addCleanup(blacklist.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(close_all)
        self.addCleanup(patcher.stop)
        store.init()


class FakeContext(JobContext):
    def __init__(self):
        class _Manager:
            lock = threading.RLock()
        super().__init__({"id": "test-job", "cancel": threading.Event(), "summary": {}, "progress": {}}, _Manager())


def option(no, size, soldout=False, price=0):
    return {"no": no, "managedCode": size, "isSoldOut": soldout, "outOfStock": soldout, "price": price,
            "optionValues": [{"name": size, "code": size, "optionNo": 1}]}


def options_payload(*items):
    return {"meta": {"result": "SUCCESS"}, "data": {"optionItems": list(items)}}


def priced(final, sale=None, coupon=0, name="쿠폰"):
    sale = final + coupon if sale is None else sale
    return {"salePrice": sale, "couponDiscount": coupon, "finalPrice": final, "priceStatus": "verified",
            "memberDiscountAmount": 0, "pointDiscountAmount": 0, "memberDiscountRate": 0.0, "memberSavePointRate": 0.0,
            "isMemberDiscount": False, "isUsePointDiscount": False, "isCouponDiscount": bool(coupon), "goodsSavePoint": 0,
            "couponName": name if coupon else "", "couponValueKind": "", "couponCount": 1 if coupon else 0,
            "baseCategoryFullName": "신발"}


@contextmanager
def fake_musinsa_session(calls=None):
    """Stand-in for pipeline.musinsa_session: no browser, records need_login."""
    def factory(need_login, ctx=None):
        @contextmanager
        def _cm():
            if calls is not None:
                calls.append(need_login)
            yield mock.Mock(name="musinsa-session"), {"cookies": []}
        return _cm()
    with mock.patch("shop_comparator.services.pipeline.musinsa_session", side_effect=factory):
        yield


@contextmanager
def fake_kream_session(error=None):
    def factory(ctx=None, need_login=False):
        @contextmanager
        def _cm():
            if error:
                raise error
            yield mock.Mock(name="kream-session"), "token"
        return _cm()
    with mock.patch("shop_comparator.services.pipeline.kream_session", side_effect=factory):
        yield


def seed_product(goods_no="1", name="테스트 상품", sizes=(("10", "250", 90000), ("11", "260", 95000)), **extra):
    store.upsert_product({"goodsNo": goods_no, "goodsName": name, "brandName": "브랜드", "serial_number": "AB-123", "rank": 1, **extra}, source="test")
    for option_no, size, price in sizes:
        values = {"size": size, "managedCode": size, "soldout": 0 if price is not None else 1, "stockCheckedAt": store.now()}
        if price is not None:
            values.update(finalPrice=price, salePrice=price, priceStatus="verified", priceSource="주문서(해당 옵션)", checkedAt=store.now())
        else:
            values.update(priceStatus="soldout")
        store.save_variant(goods_no, option_no, values)

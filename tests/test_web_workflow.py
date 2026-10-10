import io
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from openpyxl import load_workbook
from shop_comparator.shops import crawler
from shop_comparator.shops import kream_crawler
from shop_comparator.core.crawl_errors import CrawlError, response_json
import helpers  # noqa: F401  (keeps test logs out of logs/app.jsonl)
from shop_comparator.services.comparison_service import compare_product, product_id, size_key
from shop_comparator.services.exports import comparison_workbook
from shop_comparator.services.jobs import JobManager
from shop_comparator.web.web_app import create_app, validate_settings
from shop_comparator.shops.browser_transport import BrowserSession


class RegressionTests(unittest.TestCase):
    def test_cookie_token_is_used_for_bearer(self):
        headers = crawler._prepare_request_headers({}, "", {"cookies": [{"name": "app_atk", "value": "test-token"}]})
        self.assertEqual(headers["Authorization"], "Bearer test-token")

    def test_no_cookie_leak_in_api_log(self):
        output = io.StringIO()
        with redirect_stdout(output):
            crawler._log_api_call("test", "https://example.com", headers={"Cookie": "secret", "Authorization": "secret"})
        self.assertNotIn("secret", output.getvalue())

    def test_denied_options_are_not_sold_out(self):
        response = Mock(status_code=403)
        with self.assertRaises(CrawlError) as error:
            response_json(response)
        self.assertEqual(error.exception.code, "blocked")
        with patch.object(crawler, "fetch_options", side_effect=error.exception), redirect_stdout(io.StringIO()):
            with self.assertRaises(CrawlError):
                crawler.fetch_options_any(Mock(headers={}), "1")

    def test_each_option_uses_own_order_price_and_coupon(self):
        options = {"data": {"optionItems": [{"no": 10, "managedCode": "250"}, {"no": 11, "managedCode": "260"}]}}
        order = {"cartItems": [
            {"goodsNo": 1, "goodsOptionNo": 10, "salePrice": 100000, "isCouponDiscount": True},
            {"goodsNo": 1, "goodsOptionNo": 11, "salePrice": 120000, "isCouponDiscount": True}]}
        with patch.object(crawler, "add_to_cart_raw", return_value={"data": {"cartIds": [1]}}) as cart, \
             patch.object(crawler, "fetch_order_form", return_value=order), \
             patch.object(crawler, "fetch_goods_coupons", side_effect=lambda *args, **kw: [{"salePrice": int(args[2] * .1)}]), \
             redirect_stdout(io.StringIO()):
            prices = []
            for item in options["data"]["optionItems"]:
                row = crawler.process_one(Mock(), "cookie", "1", "url", {}, sleep_each=0,
                    selected_option=(item["managedCode"], str(item["no"]), item), options_payload=options, known_serial="model")
                prices.append(row["finalPrice"])
            self.assertEqual(prices, [90000, 108000])
            self.assertEqual([call.args[2] for call in cart.call_args_list], ["10", "11"])

    def test_failed_member_price_never_uses_public_price(self):
        opts = {"data": {"optionItems": [{"no": 10, "managedCode": "250"}]}}
        with patch.object(crawler, "add_to_cart_raw", side_effect=CrawlError("무신사", "auth", "expired")), redirect_stdout(io.StringIO()):
            row = crawler.process_one(Mock(), "cookie", "1", "url", {"couponPrice": 50000}, sleep_each=0,
                options_payload=opts, known_serial="model")
        self.assertIsNone(row["finalPrice"])
        self.assertEqual(row["publicPrice"], 50000)
        self.assertEqual(row["priceStatus"], "error")

    def test_wrong_size_order_item_is_rejected(self):
        opts = {"data": {"optionItems": [{"no": 11, "managedCode": "260"}]}}
        with patch.object(crawler, "add_to_cart_raw", return_value={"data": {"cartIds": [1]}}), \
             patch.object(crawler, "fetch_order_form", return_value={"cartItems": [{"goodsNo": 1, "goodsOptionNo": 10, "salePrice": 50000}]}), redirect_stdout(io.StringIO()):
            row = crawler.process_one(Mock(), "cookie", "1", "url", {}, sleep_each=0, options_payload=opts, known_serial="model")
        self.assertIsNone(row["finalPrice"])

    def test_kream_lowest_instant_price_not_first_or_bid(self):
        def item(price, action):
            return {"title_item": {"text": str(price)}, "actions": [{"type": "named_action", "value": action, "parameters": {"option_key": ["260"]}}]}
        data = {"items": [item(120000, "instant_buy"), item(100000, "instant_buy"), item(50000, "bid_buy")]}
        with patch.object(kream_crawler, "_load_tokens", return_value=("token", None, None, None, None, None)), \
             patch.object(kream_crawler, "run_curl_json", return_value=data):
            self.assertEqual(kream_crawler.get_prices("1", [], strict=True), {"260": 100000})

    def test_unknown_size_system_not_guessed(self):
        self.assertNotEqual(size_key("US 8"), size_key("UK 8"))
        match = {"kream_product_id": "2", "match_verified": 1, "match_method": "manual"}
        quote = lambda size: [{"size": size, "instantBuy": 100000, "instantSell": None, "bidOnly": False}]
        row = compare_product({"goodsNo": 1}, [{"size": "260mm", "finalPrice": 90000, "priceStatus": "verified"}], match, quote("260"))[0]
        self.assertEqual(row["difference"], 10000)
        rows = compare_product({"goodsNo": 1}, [{"size": "US 8", "finalPrice": 90000, "priceStatus": "verified"}], match, quote("UK 8"))
        self.assertIsNone(rows[0]["difference"])
        self.assertEqual(rows[0]["status"], "KREAM에 없는 사이즈")

    def test_kream_parameter_price_and_buy_action_variant(self):
        payload = {"title_item": {"text": "즉시 구매"}, "actions": [{"type": "named_action", "value": "buy",
                   "parameters": {"option_key": ["250"], "price": ["99000"]}}]}
        result = kream_crawler._extract_prices_recursive(payload, ("instant_buy",))
        self.assertEqual(result[0]["price"], 99000)
        self.assertEqual(result[0]["action"], "instant_buy")

    def test_export_keeps_text_safe_and_money_numeric(self):
        sheet = load_workbook(comparison_workbook([{"goodsNo": "1", "goodsName": "=1+1", "musinsaPrice": 10000, "kreamBuy": None}])).active
        header = {cell.value: cell.column_letter for cell in sheet[1]}
        self.assertEqual(sheet[header["상품명"] + "2"].data_type, "s")
        self.assertEqual(sheet[header["무신사 회원가"] + "2"].value, 10000)
        self.assertIsNone(sheet[header["KREAM 즉시구매 최저가"] + "2"].value)


class WebTests(helpers.TempDbTestCase):
    def setUp(self):
        super().setUp()
        self.jobs = JobManager(persist=False)
        self.app = create_app(self.jobs)
        self.client = self.app.test_client()
        import re
        html = self.client.get("/").text
        self.token = re.search(r'name="csrf-token" content="([^"]+)"', html)[1]

    def test_host_and_csrf_protection(self):
        self.assertEqual(self.client.get("/", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/stop").status_code, 403)
        self.assertEqual(self.client.post("/api/stop", headers={"X-CSRF-Token": self.token}).status_code, 200)

    def test_validation_and_empty_export(self):
        self.assertEqual(self.client.get("/api/export/comparison.xlsx").status_code, 409)
        response = self.client.post("/api/jobs/musinsa", json={"mode": "products", "products": ""}, headers={"X-CSRF-Token": self.token})
        self.assertEqual(response.status_code, 400)
        with self.assertRaises(ValueError):
            product_id("https://evil.example/products/123", "musinsa")
        data = validate_settings({"products": "https://www.musinsa.com/products/123 https://kream.co.kr/products/456"})
        self.assertEqual(data["kream_ids"], {"123": "456"})

    def test_single_job_and_cancellation(self):
        entered, finished = threading.Event(), threading.Event()
        def work(ctx):
            entered.set()
            ctx.cancel.wait(3)
            finished.set()
        job_id, _ = self.jobs.start("crawl", "수집", ["musinsa"], work)
        self.assertTrue(job_id)
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.jobs.start("crawl", "수집", ["musinsa"], work)[0], None)
        self.jobs.stop(job_id)
        self.assertTrue(finished.wait(1))


class BrowserTransportTests(unittest.TestCase):
    def test_order_request_uses_own_origin_and_never_retries_post(self):
        session = BrowserSession.__new__(BrowserSession)
        session.service, session.host = "musinsa", "www.musinsa.com"
        session.headers, session.observed = {}, {}
        session.pages = {"www.musinsa.com": "main"}
        driver = session.driver = Mock()
        driver.current_url = "https://cart.musinsa.com/api2/cart/v1/cart"
        driver.current_window_handle = "cart"
        driver.get_log.return_value = []
        driver.execute_async_script.return_value = {"status": 200, "body": "{}", "headers": {}}
        session.post(driver.current_url, headers={"Cookie": "private", "User-Agent": "old"}, json={"goodsNo": 1})
        self.assertEqual(driver.get.call_count, 1)
        self.assertEqual(driver.execute_async_script.call_count, 1)
        args = driver.execute_async_script.call_args.args
        self.assertEqual(args[2], "POST")
        self.assertNotIn("cookie", args[3])
        self.assertNotIn("user-agent", args[3])
        with self.assertRaises(ValueError):
            session.get("https://evil.example/api")

    def test_browser_denial_is_not_retried(self):
        session = BrowserSession.__new__(BrowserSession)
        session.service, session.host = "musinsa", "www.musinsa.com"
        session.headers, session.observed = {}, {}
        session.pages = {"www.musinsa.com": "main"}
        session.driver = Mock()
        session.driver.get_log.return_value = []
        session.driver.execute_async_script.return_value = {"status": 403, "body": "blocked", "headers": {}}
        with self.assertRaises(CrawlError):
            response_json(session.get("https://goods-detail.musinsa.com/api"))
        self.assertEqual(session.driver.execute_async_script.call_count, 1)


if __name__ == "__main__":
    unittest.main()

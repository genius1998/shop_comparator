"""Collection stages with mocked shop responses (no real site is contacted)."""
import unittest
from unittest import mock

from shop_comparator.services import comparison_service as cmp
from shop_comparator.shops import crawler
from shop_comparator.shops import kream_api
from shop_comparator.services import pipeline
from shop_comparator.storage import store
from shop_comparator.core.crawl_errors import CrawlError
from helpers import (FakeContext, TempDbTestCase, fake_kream_session, fake_musinsa_session, fixture, option,
                     options_payload, priced, seed_product)

META = {"goodsNo": 1, "goodsName": "러너 250", "brandName": "브랜드", "imageUrl": "https://image.msscdn.net/x.jpg", "normalPrice": 100000}


def musinsa_patches(options, price_side_effect):
    return [
        mock.patch.object(pipeline, "REQUEST_GAP", 0),
        mock.patch.object(crawler, "fetch_details", return_value={"1": META}),
        mock.patch.object(crawler, "fetch_options_any", return_value=options),
        mock.patch.object(crawler, "_fetch_serial_number_robust", return_value="AB-123"),
        mock.patch.object(crawler, "price_option", side_effect=price_side_effect),
    ]


class MusinsaCollectTests(TempDbTestCase):
    def run_collect(self, options, price, mode="per_option", ids=("1",)):
        patches = musinsa_patches(options, price)
        started = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        ctx = FakeContext()
        with fake_musinsa_session():
            pipeline.collect_musinsa({"mode": "products", "ids": list(ids), "limit": 10, "price_mode": mode}, ctx)
        return ctx, started[-1]

    def test_every_size_is_stored_and_each_option_gets_its_own_price(self):
        options = options_payload(option(10, "250"), option(11, "260", price=5000), option(12, "270", soldout=True))
        prices = {"10": priced(90000, coupon=10000), "11": priced(108000, coupon=12000)}
        ctx, price_option = self.run_collect(options, lambda cookie, goods, opt, **kw: prices[opt])
        rows = {v["size"]: v for v in store.variants("1")}
        self.assertEqual(set(rows), {"250", "260", "270"})
        self.assertEqual((rows["250"]["finalPrice"], rows["260"]["finalPrice"]), (90000, 108000))
        self.assertEqual([c.args[2] for c in price_option.call_args_list], ["10", "11"])  # sold-out size never priced
        self.assertEqual(rows["270"]["priceStatus"], "soldout")
        self.assertIsNone(rows["270"]["finalPrice"])
        self.assertEqual(rows["250"]["priceSource"], "주문서(해당 옵션)")
        self.assertEqual(ctx.summary["ok"], 1)

    def test_failed_lookup_is_an_error_not_sold_out_and_never_a_public_price(self):
        options = options_payload(option(10, "250"), option(11, "260"))

        def price(cookie, goods, opt, **kw):
            if opt == "11":
                raise CrawlError("무신사", "pricing", "주문 폼에서 상품을 찾을 수 없음")
            return priced(90000)
        ctx, _ = self.run_collect(options, price)
        rows = {v["size"]: v for v in store.variants("1")}
        self.assertEqual(rows["260"]["priceStatus"], "error")
        self.assertEqual(rows["260"]["errorKind"], "pricing")
        self.assertIsNone(rows["260"]["finalPrice"])
        self.assertEqual(rows["260"]["soldout"], 0)
        self.assertEqual(rows["250"]["finalPrice"], 90000)  # the other size is unaffected
        status = {r["size"]: r["status"] for r in cmp.build(["1"])}
        self.assertEqual(status["260"], "회원가 확인 실패")
        self.assertEqual(ctx.summary["failed"], 1)

    def test_refresh_failure_keeps_last_verified_price_as_stale(self):
        options = options_payload(option(10, "250"))
        self.run_collect(options, lambda *a, **k: priced(90000))
        before = store.variants("1")[0]
        with mock.patch.object(crawler, "price_option", side_effect=CrawlError("무신사", "schema", "판매가 없음")), \
             fake_musinsa_session():
            pipeline.collect_musinsa({"mode": "selected", "ids": ["1"], "limit": 1, "price_mode": "per_option"}, FakeContext())
        after = store.variants("1")[0]
        self.assertEqual(after["finalPrice"], 90000)
        self.assertEqual(after["priceStatus"], "stale")
        self.assertEqual(after["checkedAt"], before["checkedAt"])
        self.assertIn("판매가", after["error"])
        self.assertEqual(cmp.build(["1"])[0]["status"].split(" · ")[0], "회원가 갱신 실패(이전 값)")

    def test_auth_error_stops_the_job_and_keeps_saved_sizes(self):
        options = options_payload(option(10, "250"), option(11, "260"), option(12, "270"))
        calls = []

        def price(cookie, goods, opt, **kw):
            calls.append(opt)
            if opt == "11":
                raise CrawlError("무신사", "auth", "로그인이 만료되었습니다. 다시 로그인해 주세요 (401).")
            return priced(90000)
        patches = musinsa_patches(options, price)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with fake_musinsa_session(), self.assertRaises(CrawlError) as raised:
            pipeline.collect_musinsa({"mode": "products", "ids": ["1"], "limit": 1, "price_mode": "per_option"}, FakeContext())
        self.assertEqual(raised.exception.code, "auth")
        self.assertEqual(calls, ["10", "11"])  # no further requests after the auth failure
        self.assertEqual({v["size"]: v["finalPrice"] for v in store.variants("1")}, {"250": 90000, "260": None})
        self.assertEqual(store.sessions()["musinsa"]["state"], "expired")

    def test_stock_only_mode_needs_no_login_and_never_prices(self):
        need_login = []
        patches = musinsa_patches(options_payload(option(10, "250")), AssertionError("must not price"))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with fake_musinsa_session(need_login):
            pipeline.collect_musinsa({"mode": "products", "ids": ["1"], "limit": 1, "price_mode": "none"}, FakeContext())
        self.assertEqual(need_login, [False])
        self.assertEqual(cmp.build(["1"])[0]["status"], "회원가 미조회")

    def test_grouped_mode_reuses_only_equal_option_prices_and_says_so(self):
        options = options_payload(option(10, "250", price=0), option(11, "260", price=0), option(12, "270", price=5000))
        prices = {"10": priced(90000), "12": priced(95000)}
        _, price_option = self.run_collect(options, lambda cookie, goods, opt, **kw: prices[opt], mode="grouped")
        rows = {v["size"]: v for v in store.variants("1")}
        self.assertEqual([c.args[2] for c in price_option.call_args_list], ["10", "12"])
        self.assertEqual(rows["260"]["finalPrice"], 90000)
        self.assertIn("대표 옵션 10", rows["260"]["priceSource"])
        self.assertEqual(rows["270"]["finalPrice"], 95000)

    def test_collecting_one_product_does_not_remove_others_or_their_matches(self):
        seed_product("2", "다른 상품")
        store.set_manual_match("2", "777")
        self.run_collect(options_payload(option(10, "250")), lambda *a, **k: priced(90000))
        self.assertEqual({str(p["goodsNo"]) for p in store.products()}, {"1", "2"})
        self.assertEqual(len(store.variants("2")), 2)
        self.assertEqual(store.product("2")["kream_product_id"], "777")

    def test_unorderable_product_is_asked_once_and_does_not_stop_the_batch(self):
        options = options_payload(*[option(10 + i, str(250 + 5 * i)) for i in range(6)])
        calls = []

        def price(cookie, goods, opt, **kw):
            calls.append((goods, opt))
            if goods == "1":
                raise CrawlError("무신사", "api", "API가 실패를 반환했습니다: 판매가 종료된 상품이 포함되어 있습니다. [ORDER-111-0005]")
            return priced(90000)
        patches = musinsa_patches(options, price)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        crawler.fetch_details.return_value = {"1": META, "2": {**META, "goodsNo": 2}}
        ctx = FakeContext()
        with fake_musinsa_session():
            pipeline.collect_musinsa({"mode": "products", "ids": ["1", "2"], "limit": 2, "price_mode": "per_option"}, ctx)
        self.assertEqual([c for c in calls if c[0] == "1"], [("1", "10")])        # one request, not six
        self.assertEqual(len([c for c in calls if c[0] == "2"]), 6)                # next product still collected
        blocked = store.variants("1")
        self.assertTrue(all(v["errorKind"] == "unorderable" and v["finalPrice"] is None and not v["soldout"] for v in blocked))
        self.assertTrue(all(v["finalPrice"] == 90000 for v in store.variants("2")))
        self.assertEqual((ctx.summary["failed"], ctx.summary["ok"]), (1, 1))

    def test_repeated_failures_abort_instead_of_hammering(self):
        options = options_payload(*[option(10 + i, str(250 + 5 * i)) for i in range(9)])
        calls = []

        def price(cookie, goods, opt, **kw):
            calls.append(opt)
            raise CrawlError("무신사", "pricing", "카트 담기 실패")
        patches = musinsa_patches(options, price)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with fake_musinsa_session(), self.assertRaises(CrawlError):
            pipeline.collect_musinsa({"mode": "products", "ids": ["1"], "limit": 1, "price_mode": "per_option"}, FakeContext())
        self.assertEqual(len(calls), pipeline.MAX_CONSECUTIVE_FAILURES)


class KreamStageTests(TempDbTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(FakeContext, "wait", lambda self, seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def quotes(self, **prices):
        return [{"size": size, "instantBuy": buy, "instantBuyDelivery": "일반배송", "instantSell": None, "bidOnly": buy is None, "candidates": []}
                for size, buy in prices.items()]

    def test_prerequisites_are_explained(self):
        with self.assertRaises(CrawlError) as empty:
            pipeline.collect_kream(None, FakeContext())
        self.assertEqual(empty.exception.code, "prerequisite")
        self.assertIn("무신사", str(empty.exception))
        seed_product()
        with self.assertRaises(CrawlError) as unmatched:
            pipeline.collect_kream(None, FakeContext())
        self.assertEqual(unmatched.exception.code, "prerequisite")
        self.assertIn("매칭", str(unmatched.exception))

    def test_kream_only_run_reuses_stored_musinsa_results(self):
        seed_product()
        store.set_manual_match("1", "500")
        with fake_kream_session(), mock.patch.object(kream_api, "fetch_quotes", return_value=self.quotes(**{"250": 100000, "W260": 99000})), \
             mock.patch.object(crawler, "fetch_options_any", side_effect=AssertionError("Musinsa must not be contacted")), \
             mock.patch.object(crawler, "price_option", side_effect=AssertionError("Musinsa must not be contacted")):
            pipeline.collect_kream(None, FakeContext())
        rows = {r["size"]: r for r in cmp.build(["1"])}
        self.assertEqual(rows["250"]["difference"], 10000)   # 100000 - 90000
        self.assertEqual(rows["260"]["difference"], 4000)    # W260 matches 260 (same mm system)
        self.assertEqual(rows["250"]["status"], "비교 완료")
        self.assertEqual(rows["250"]["matchStatus"], "직접 지정")

    def test_kream_failure_keeps_previous_quotes_and_musinsa_data(self):
        seed_product()
        store.set_manual_match("1", "500")
        store.save_kream_quotes("500", self.quotes(**{"250": 100000}))
        with fake_kream_session(), mock.patch.object(kream_api, "fetch_quotes", side_effect=CrawlError("KREAM", "http", "서버 응답 오류 (500).")):
            ctx = FakeContext()
            pipeline.collect_kream(None, ctx)
        self.assertEqual(ctx.summary["failed"], 1)
        row = {r["size"]: r for r in cmp.build(["1"])}["250"]
        self.assertEqual((row["musinsaPrice"], row["kreamBuy"]), (90000, 100000))
        self.assertIn("500", row["kreamError"])
        self.assertEqual(store.product("1")["kreamStatus"], "error")

    def test_dropped_connection_is_retried_once_then_stops_instead_of_racing_through(self):
        for goods_no in ("1", "2", "3", "4", "5"):
            seed_product(goods_no, f"상품 {goods_no}")
            store.set_manual_match(goods_no, str(500 + int(goods_no)))
        calls = []

        def flaky(kream_id, token=None):
            calls.append(kream_id)
            if kream_id == "501" and calls.count("501") == 1:
                raise CrawlError("KREAM", "network", "브라우저 요청에 실패했습니다.")
            if kream_id == "501":
                return self.quotes(**{"250": 100000})
            raise CrawlError("KREAM", "network", "브라우저 요청에 실패했습니다.")
        ctx = FakeContext()
        with fake_kream_session(), mock.patch.object(kream_api, "fetch_quotes", side_effect=flaky), self.assertRaises(CrawlError) as raised:
            pipeline.collect_kream(None, ctx)
        self.assertEqual(calls[:2], ["501", "501"])                 # retried once and recovered
        self.assertEqual(ctx.summary["ok"], 1)
        self.assertEqual(ctx.summary["failed"], 3)                   # stops after three in a row
        self.assertNotIn("505", calls)
        self.assertIn("3회 연속", str(raised.exception))

    def test_kream_login_failure_does_not_touch_musinsa(self):
        seed_product()
        store.set_manual_match("1", "500")
        with fake_kream_session(CrawlError("KREAM", "auth", "KREAM 로그인이 필요합니다.")), self.assertRaises(CrawlError):
            pipeline.collect_kream(None, FakeContext())
        self.assertEqual([v["finalPrice"] for v in store.variants("1")], [90000, 95000])

    def test_unverified_match_is_not_priced(self):
        seed_product()
        from shop_comparator.storage import database
        database.cache_kream_search("1", "500", "https://kream.co.kr/products/500", match_verified=False)
        with mock.patch.object(kream_api, "fetch_quotes", return_value=self.quotes(**{"250": 1})) as fetch:
            ctx = FakeContext()
            pipeline.quote_products(store.products(goods_nos=["1"]), "token", ctx)
        fetch.assert_not_called()
        self.assertEqual(ctx.summary["unverified"], 1)
        self.assertEqual(cmp.build(["1"])[0]["status"], "매칭 검증 필요")

    def test_matching_respects_blacklist_and_duplicate_ids(self):
        seed_product("1")
        seed_product("2", "다른 상품")
        store.set_manual_match("2", "500")
        from shop_comparator.shops import kream_crawler
        found = kream_crawler.KreamSearchResult(500, match_verified=True, match_method="style_code", match_confidence=0.97, match_reason="모델번호 일치")
        with mock.patch.object(pipeline, "_blacklist", return_value=set()), mock.patch.object(pipeline, "_blacklist_add") as add, \
             mock.patch.object(kream_crawler, "search_product", return_value=found):
            ctx = FakeContext()
            pipeline.match_products(store.products(goods_nos=["1"]), ctx)
            self.assertEqual(ctx.summary["duplicate"], 1)  # 500 already belongs to product 2
            add.assert_not_called()
        with mock.patch.object(pipeline, "_blacklist", return_value={"1"}), mock.patch.object(kream_crawler, "search_product") as search:
            ctx = FakeContext()
            pipeline.match_products(store.products(goods_nos=["1"]), ctx)
            search.assert_not_called()
            self.assertEqual(ctx.summary["blacklisted"], 1)

    def test_rule_match_is_stored_with_confidence(self):
        seed_product("1")
        from shop_comparator.shops import kream_crawler
        found = kream_crawler.KreamSearchResult(321, match_verified=True, match_method="local", match_confidence=0.97, match_reason="모델번호 일치")
        with mock.patch.object(pipeline, "_blacklist", return_value=set()), mock.patch.object(kream_crawler, "search_product", return_value=found):
            pipeline.match_products(store.products(goods_nos=["1"]), FakeContext())
        info = cmp.match_info(store.match_for("1"))
        self.assertEqual((info["kreamId"], info["status"], info["confidence"], info["usable"]), ("321", "규칙 확정", 0.97, True))


class AlertTests(TempDbTestCase):
    def test_discord_alert_uses_the_selected_gap_and_margin(self):
        seed_product("1", "러너", sizes=(("10", "250", 90000), ("11", "260", 90000)))
        quotes = [{"size": "250", "instantBuy": 105000, "instantSell": 95000}, {"size": "260", "instantBuy": 95000, "instantSell": 80000}]
        store.set_setting("webhook_url", "https://discord.com/api/webhooks/1/x")
        store.set_setting("alert_margin", 10000)
        sent = []
        with mock.patch.object(pipeline.webhook, "send_discord_notification", side_effect=lambda url, message: sent.append(message) or True):
            pipeline._notify_margin(store.product("1"), "500", quotes)                 # default: KREAM buy - Musinsa
            self.assertEqual(len(sent), 1)
            self.assertIn("**사이즈:** 250", sent[0])
            self.assertIn("15,000원", sent[0])
            store.set_setting("alert_basis", "kream_sell_minus_musinsa")              # 95,000 - 90,000 < margin
            pipeline._notify_margin(store.product("1"), "500", quotes)
            self.assertEqual(len(sent), 1)
            store.set_setting("alert_basis", "musinsa_minus_kream_buy")               # GUI rule: no size is cheaper on KREAM by 10,000
            pipeline._notify_margin(store.product("1"), "500", quotes)
            self.assertEqual(len(sent), 1)
            store.set_setting("webhook_url", "")
            pipeline._notify_margin(store.product("1"), "500", quotes)
            self.assertEqual(len(sent), 1)


class FullRunTests(TempDbTestCase):
    def test_kream_failure_in_full_run_keeps_musinsa_results(self):
        patches = musinsa_patches(options_payload(option(10, "250")), lambda *a, **k: priced(90000))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        ctx = FakeContext()
        with fake_musinsa_session(), fake_kream_session(CrawlError("KREAM", "auth", "KREAM 로그인이 필요합니다.")):
            pipeline.full({"mode": "products", "ids": ["1"], "limit": 1, "price_mode": "per_option"}, ctx)
        self.assertEqual(store.variants("1")[0]["finalPrice"], 90000)
        self.assertTrue(any("KREAM" in e for e in ctx.summary["stageErrors"]))
        self.assertEqual(ctx.summary["rows"], 1)

    def test_compare_requires_musinsa_results(self):
        with self.assertRaises(CrawlError) as raised:
            pipeline.compare(None, FakeContext())
        self.assertEqual(raised.exception.code, "prerequisite")


class KreamParserTests(unittest.TestCase):
    """Uses excerpts of real KREAM responses saved during earlier runs."""

    def test_lowest_new_condition_instant_price_across_delivery_types(self):
        buy = kream_api.parse_buy(fixture("kream_buy_15251_excerpt.json"))
        self.assertEqual(buy["220"]["instantBuy"], 76000)          # 일반배송 76,000 < 해외배송 116,000
        self.assertEqual(buy["220"]["instantBuyDelivery"], "일반배송")
        self.assertEqual([c["price"] for c in buy["220"]["candidates"]], [76000, 116000])
        self.assertNotIn(85000, [c["price"] for c in buy["220"]["candidates"]])  # 95점(quality 3) 제외
        self.assertEqual(buy["215"]["instantBuy"], 120000)

    def test_bid_only_size_has_no_instant_price(self):
        payload = {"content": {"items": [{"title_item": {"text": "구매 입찰하기"}, "actions": [
            {"type": "named_action", "value": "bid_buy", "parameters": {"option_key": ["300"]}}]}]}}
        quote = kream_api.parse_buy(payload)["300"]
        self.assertIsNone(quote["instantBuy"])
        self.assertTrue(quote["bidOnly"])

    def test_buy_action_without_instant_wording_is_a_bid(self):
        payload = {"items": [{"title_item": {"text": "구매 입찰"}, "actions": [
            {"type": "named_action", "value": "buy", "parameters": {"option_key": ["250"], "price": ["50000"]}}]}]}
        self.assertIsNone(kream_api.parse_buy(payload)["250"]["instantBuy"])

    def test_brand_shipped_product_uses_displayed_size_not_option_id(self):
        payload = {"content": {"items": [
            {"actions": [{"type": "named_action", "value": "select_option",
                          "parameters": {"option_key": ["761964"], "option_name_display": ["240"]}}]},
            {"title_item": {"text": "즉시 구매하기"}, "actions": [{"type": "named_action", "value": "instant_buy",
                          "parameters": {"option_key": ["761964"], "price": ["69000"], "delivery_method": ["brand"], "quality": ["1"]}}]}]}}
        quote = kream_api.parse_buy(payload)
        self.assertEqual(list(quote), ["240"])
        self.assertEqual((quote["240"]["instantBuy"], quote["240"]["instantBuyDelivery"]), (69000, "브랜드배송"))

    def test_sell_side_is_separate_from_buy_side(self):
        sell = kream_api.parse_sell(fixture("kream_sell_15251_excerpt.json"))
        self.assertEqual(sell, {"310": None, "305": 110000, "300": 107000})
        merged = {q["size"]: q for q in kream_api.merge({"300": {"instantBuy": 120000}}, sell)}
        self.assertEqual((merged["300"]["instantBuy"], merged["300"]["instantSell"]), (120000, 107000))
        self.assertIsNone(merged["305"]["instantBuy"])


if __name__ == "__main__":
    unittest.main()

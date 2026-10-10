# -*- coding: utf-8 -*-

import unittest

from shop_comparator.shops import crawler


class CrawlerPricingTests(unittest.TestCase):
    def test_pick_best_coupon_uses_largest_discount_amount(self):
        coupon = crawler.pick_best_coupon(
            [
                {"couponName": "small", "salePrice": 1000},
                {"couponName": "large", "salePrice": 4500},
                {"couponName": "middle", "salePrice": 3000},
            ]
        )

        self.assertEqual(coupon["couponName"], "large")

    def test_compute_final_v2_applies_coupon_before_member_and_point_discount(self):
        result = crawler.compute_final_v2(
            sale_price=100000,
            coupon_discount=5000,
            flags={
                "isMemberDiscount": True,
                "memberDiscountRate": 5,
                "isUsePointDiscount": True,
                "goodsSavePoint": 1000,
                "memberSavePointRate": 2,
            },
            member_info={"maximumUsePointRatio": 7, "totalPoint": 100000},
        )

        self.assertEqual(result["after_coupon"], 95000)
        self.assertEqual(result["member_discount_amount"], 4750)
        self.assertEqual(result["point_discount_amount"], 2800)
        self.assertEqual(result["final_price"], 87450)


if __name__ == "__main__":
    unittest.main()

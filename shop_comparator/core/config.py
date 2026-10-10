# -*- coding: utf-8 -*-

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:142.0) Gecko/20100101 Firefox/142.0"

RANKING_PAGE   = "https://www.musinsa.com/main/musinsa/ranking?gf=A&storeCode=musinsa&sectionId=199&contentsId=&categoryCode=103000&ageBand=AGE_BAND_ALL"
RANKING_LAYOUT = "https://api.musinsa.com/api2/hm/web/v5/pans/ranking?storeCode=musinsa&sectionId=199&gf=A&contentsId=&categoryCode=103000&ageBand=AGE_BAND_ALL"

# Ranking-based crawling URLs
SHOES_RANKING_LAYOUT = "https://api.musinsa.com/api2/hm/web/v5/pans/ranking?storeCode=musinsa&sectionId=199&gf=A&contentsId=&categoryCode=103000&ageBand=AGE_BAND_ALL"
BOUTIQUE_RANKING_LAYOUT = "https://api.musinsa.com/api2/hm/web/v5/pans/ranking?storeCode=boutique&sectionId=232&gf=A&contentsId=&categoryCode=105000&ageBand=AGE_BAND_ALL"
BOUTIQUE_WALLET_BAG_RANKING_LAYOUT = "https://api.musinsa.com/api2/hm/web/v5/pans/ranking?storeCode=boutique&sectionId=232&gf=A&contentsId=&categoryCode=105003&ageBand=AGE_BAND_ALL"
BOUTIQUE_ACCESSORY_RANKING_LAYOUT = "https://api.musinsa.com/api2/hm/web/v5/pans/ranking?storeCode=boutique&sectionId=232&gf=A&contentsId=&categoryCode=105004&ageBand=AGE_BAND_ALL"

DETAILS_BASE   = "https://api.musinsa.com/api2/dp/v1/goods"

PRODUCT_URL      = "https://www.musinsa.com/products/{goods_no}"
OPTIONS_URL      = "https://goods-detail.musinsa.com/api2/goods/{goods_no}/options"
INVENTORY_URL    = "https://goods-detail.musinsa.com/api2/goods/{goods_no}/options/prioritized-inventories"
CART_SAVE_URL    = "https://cart.musinsa.com/api2/cart/v1/cart"
ORDER_FORM_URL   = "https://order.musinsa.com/api2/order/v1/orders/order-form"
GOODS_COUPON_URL = "https://order.musinsa.com/api2/order/v1/goods-coupons"
PAYMENT_EVENT_URL = "https://order.musinsa.com/api2/order/v1/payment/event"  # 결제 프로모션

DB_FILE = "musinsa_data.sqlite"
KREAM_COOKIES_FILE = "cookies.json"
KREAM_TOKEN_FILE = "cookies_token.json"

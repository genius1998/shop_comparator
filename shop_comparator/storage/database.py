# -*- coding: utf-8 -*-

from __future__ import annotations

import sqlite3
import json
from shop_comparator.core.config import DB_FILE
from shop_comparator.core.utils import get_persistent_path

def get_db_conn():
    db_path = get_persistent_path(DB_FILE)
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db_conn() as conn:
        c = conn.cursor()
        # 쿠키 테이블: 최신 1개만 저장
        c.execute("""
        CREATE TABLE IF NOT EXISTS cookies (
            id INTEGER PRIMARY KEY, 
            cookie_header TEXT NOT NULL, 
            auth_payload TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        c.execute("PRAGMA table_info(cookies)")
        cookie_columns = [col[1] for col in c.fetchall()]
        if "auth_payload" not in cookie_columns:
            c.execute("ALTER TABLE cookies ADD COLUMN auth_payload TEXT")
        # 상품 테이블
        c.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rank INTEGER,
            goodsNo TEXT UNIQUE,
            linkUrl TEXT,
            goodsName TEXT,
            brandName TEXT,
            serial_number TEXT,
            imageUrl TEXT,
            normalPrice INTEGER,
            saleRate INTEGER,
            salePrice INTEGER,
            couponDiscount INTEGER,
            finalPrice INTEGER,
            availableSizes TEXT,
            soldout BOOLEAN,
            baseCategoryFullName TEXT,
            memberDiscountRate REAL,
            memberSavePointRate REAL,
            isMemberDiscount BOOLEAN,
            isUsePointDiscount BOOLEAN,
            goodsSavePoint INTEGER,
            couponName TEXT,
            couponValueKind TEXT,
            managedCode TEXT,
            goodsOptionNo TEXT,
            kream_product_id TEXT, -- Kream product ID
            kream_product_url TEXT, -- Kream product URL
            crawled_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # Add new columns if they don't exist
        c.execute("PRAGMA table_info(products)")
        columns = [col[1] for col in c.fetchall()]
        if 'memberDiscountAmount' not in columns:
            c.execute("ALTER TABLE products ADD COLUMN memberDiscountAmount INTEGER")
        if 'pointDiscountAmount' not in columns:
            c.execute("ALTER TABLE products ADD COLUMN pointDiscountAmount INTEGER")
        if 'lastObservedFinalPrice' not in columns:
            c.execute("ALTER TABLE products ADD COLUMN lastObservedFinalPrice INTEGER")
        if 'kream_total_transactions' not in columns:
            c.execute("ALTER TABLE products ADD COLUMN kream_total_transactions INTEGER DEFAULT 0")
        if 'kream_monthly_transactions' not in columns:
            c.execute("ALTER TABLE products ADD COLUMN kream_monthly_transactions INTEGER DEFAULT 0")
        # Kream 사이즈별 가격 테이블
        c.execute("""
        CREATE TABLE IF NOT EXISTS kream_size_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kream_product_id TEXT NOT NULL,
            size TEXT NOT NULL,
            price INTEGER NULL, -- Explicitly allow NULL
            crawled_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(kream_product_id, size) -- 동일 상품-사이즈 중복 방지
        )
        """)
        # Kream 사이즈별 가격 테이블2
        c.execute("""
        CREATE TABLE IF NOT EXISTS kream_size_prices_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kream_product_id TEXT NOT NULL,
            size TEXT NOT NULL,
            price INTEGER NULL,
            crawled_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(kream_product_id, size) -- 동일 상품-사이즈 중복 방지
            )
        """)
        # Kream 검색 결과 캐시 테이블
        c.execute("""
        CREATE TABLE IF NOT EXISTS kream_search_cache (
            musinsa_goods_no TEXT PRIMARY KEY,
            musinsa_goods_name TEXT,
            kream_product_id TEXT,
            kream_product_url TEXT,
            kream_brand TEXT,
            kream_product_name TEXT,
            kream_image_url TEXT,
            kream_all_text TEXT,
            match_verified INTEGER DEFAULT 0,
            match_method TEXT,
            match_confidence REAL,
            match_reason TEXT,
            verified_at DATETIME,
            last_searched_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # Add new column if it doesn't exist
        c.execute("PRAGMA table_info(kream_search_cache)")
        columns = [col[1] for col in c.fetchall()]
        if 'musinsa_goods_name' not in columns:
            c.execute("ALTER TABLE kream_search_cache ADD COLUMN musinsa_goods_name TEXT")
        if 'match_verified' not in columns:
            c.execute("ALTER TABLE kream_search_cache ADD COLUMN match_verified INTEGER DEFAULT 0")
        if 'match_method' not in columns:
            c.execute("ALTER TABLE kream_search_cache ADD COLUMN match_method TEXT")
        if 'match_confidence' not in columns:
            c.execute("ALTER TABLE kream_search_cache ADD COLUMN match_confidence REAL")
        if 'match_reason' not in columns:
            c.execute("ALTER TABLE kream_search_cache ADD COLUMN match_reason TEXT")
        if 'verified_at' not in columns:
            c.execute("ALTER TABLE kream_search_cache ADD COLUMN verified_at DATETIME")
        if 'kream_brand' not in columns:
            c.execute("ALTER TABLE kream_search_cache ADD COLUMN kream_brand TEXT")
        if 'kream_product_name' not in columns:
            c.execute("ALTER TABLE kream_search_cache ADD COLUMN kream_product_name TEXT")
        if 'kream_image_url' not in columns:
            c.execute("ALTER TABLE kream_search_cache ADD COLUMN kream_image_url TEXT")
        if 'kream_all_text' not in columns:
            c.execute("ALTER TABLE kream_search_cache ADD COLUMN kream_all_text TEXT")

        # Kream 상품 정보 테이블 (kream_crawler.py 참조)
        c.execute("""
        CREATE TABLE IF NOT EXISTS kream_products (
            product_id INTEGER PRIMARY KEY,
            model_number TEXT,
            brand_name TEXT,
            product_name TEXT
        )
        """)
        conn.commit()

def print_kream_cache():
    print("--- [Kream Search Cache] ---")
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT musinsa_goods_no, kream_product_id, kream_product_url FROM kream_search_cache")
        rows = c.fetchall()
        if not rows:
            print("캐시된 크림 검색 결과가 없습니다.")
        for row in rows:
            print(f"  - {row['musinsa_goods_no']}: ID({row['kream_product_id']}), URL({row['kream_product_url']})")
    print("--------------------------")


def get_cached_kream_search(musinsa_goods_no: str) -> dict | None:
    init_db()
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT
                kream_product_id,
                kream_product_url,
                kream_brand,
                kream_product_name,
                kream_image_url,
                kream_all_text,
                match_verified,
                match_method,
                match_confidence,
                match_reason,
                verified_at
            FROM kream_search_cache
            WHERE musinsa_goods_no = ?
            """,
            (musinsa_goods_no,),
        )
        row = c.fetchone()
        if row:
            return dict(row)
        return None

def cache_kream_search(
    musinsa_goods_no: str,
    kream_product_id: str,
    kream_product_url: str,
    match_verified: bool | None = None,
    match_method: str | None = None,
    match_confidence: float | None = None,
    match_reason: str | None = None,
    kream_brand: str | None = None,
    kream_product_name: str | None = None,
    kream_image_url: str | None = None,
    kream_all_text: str | None = None,
):
    init_db()
    if match_verified is None:
        match_verified = bool(getattr(kream_product_id, "match_verified", False))
    if match_method is None:
        match_method = getattr(kream_product_id, "match_method", None)
    if match_confidence is None:
        match_confidence = getattr(kream_product_id, "match_confidence", None)
    if match_reason is None:
        match_reason = getattr(kream_product_id, "match_reason", None)
    if kream_brand is None:
        kream_brand = getattr(kream_product_id, "kream_brand", None)
    if kream_product_name is None:
        kream_product_name = getattr(kream_product_id, "kream_product_name", None)
    if kream_image_url is None:
        kream_image_url = getattr(kream_product_id, "kream_image_url", None)
    if kream_all_text is None:
        kream_all_text = getattr(kream_product_id, "kream_all_text", None)

    verified_int = 1 if match_verified else 0
    kream_product_id_str = str(kream_product_id)

    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT kream_product_id, match_verified FROM kream_search_cache WHERE musinsa_goods_no = ?",
            (musinsa_goods_no,),
        )
        existing = c.fetchone()
        if (
            existing
            and int(existing["match_verified"] or 0) == 1
            and not verified_int
            and str(existing["kream_product_id"]) != kream_product_id_str
        ):
            print(
                f"[DB CACHE] verified match exists; skipped unverified overwrite: "
                f"{musinsa_goods_no} {existing['kream_product_id']} != {kream_product_id_str}"
            )
            return

        c.execute("""
            INSERT INTO kream_search_cache (
                musinsa_goods_no,
                kream_product_id,
                kream_product_url,
                kream_brand,
                kream_product_name,
                kream_image_url,
                kream_all_text,
                match_verified,
                match_method,
                match_confidence,
                match_reason,
                verified_at,
                last_searched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? = 1 THEN CURRENT_TIMESTAMP ELSE NULL END, CURRENT_TIMESTAMP)
            ON CONFLICT(musinsa_goods_no) DO UPDATE SET
                kream_product_id = excluded.kream_product_id,
                kream_product_url = excluded.kream_product_url,
                kream_brand = COALESCE(excluded.kream_brand, kream_search_cache.kream_brand),
                kream_product_name = COALESCE(excluded.kream_product_name, kream_search_cache.kream_product_name),
                kream_image_url = COALESCE(excluded.kream_image_url, kream_search_cache.kream_image_url),
                kream_all_text = COALESCE(excluded.kream_all_text, kream_search_cache.kream_all_text),
                match_verified = CASE
                    WHEN excluded.match_verified = 1 THEN 1
                    ELSE kream_search_cache.match_verified
                END,
                match_method = COALESCE(excluded.match_method, kream_search_cache.match_method),
                match_confidence = COALESCE(excluded.match_confidence, kream_search_cache.match_confidence),
                match_reason = COALESCE(excluded.match_reason, kream_search_cache.match_reason),
                verified_at = CASE
                    WHEN excluded.match_verified = 1 THEN CURRENT_TIMESTAMP
                    ELSE kream_search_cache.verified_at
                END,
                last_searched_at = CURRENT_TIMESTAMP
        """, (
            musinsa_goods_no,
            kream_product_id_str,
            kream_product_url,
            kream_brand,
            kream_product_name,
            kream_image_url,
            kream_all_text,
            verified_int,
            match_method,
            match_confidence,
            match_reason,
            verified_int,
        ))
        conn.commit()

def is_kream_id_already_used(kream_product_id: str, current_goods_no: str, verified_only: bool = False) -> bool:
    """Checks if a KREAM ID is already assigned to a DIFFERENT Musinsa product in the cache."""
    init_db()
    with get_db_conn() as conn:
        c = conn.cursor()
        # kream_product_id가 문자열이 아닐 경우를 대비해 변환
        kream_product_id_str = str(kream_product_id)
        verified_filter = "AND COALESCE(match_verified, 0) = 1" if verified_only else ""
        c.execute(f"""
            SELECT 1 
            FROM kream_search_cache 
            WHERE kream_product_id = ? AND musinsa_goods_no != ? 
            {verified_filter}
            LIMIT 1
        """, (kream_product_id_str, current_goods_no))
        return c.fetchone() is not None

def save_cookie(cookie_header: str, auth_payload: dict | None = None):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM cookies")
        c.execute(
            "INSERT INTO cookies (cookie_header, auth_payload) VALUES (?, ?)",
            (cookie_header, json.dumps(auth_payload or {}, ensure_ascii=False)),
        )
        conn.commit()

def load_cookie() -> str | None:
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT cookie_header FROM cookies ORDER BY created_at DESC LIMIT 1")
        row = c.fetchone()
        return row["cookie_header"] if row else None

def load_auth_session() -> dict:
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT cookie_header, auth_payload FROM cookies ORDER BY created_at DESC LIMIT 1"
        )
        row = c.fetchone()
        if not row:
            return {"cookie_header": None, "auth_payload": {}}

        auth_payload = {}
        raw_auth_payload = row["auth_payload"]
        if raw_auth_payload:
            try:
                auth_payload = json.loads(raw_auth_payload)
            except json.JSONDecodeError:
                auth_payload = {}

        return {
            "cookie_header": row["cookie_header"],
            "auth_payload": auth_payload,
        }

def save_products(products: list[dict]):
    init_db()
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT goodsNo, lastObservedFinalPrice FROM products")
        existing_last_prices = {
            str(row["goodsNo"]): row["lastObservedFinalPrice"]
            for row in c.fetchall()
            if row["goodsNo"] is not None
        }
        # 기존 데이터 모두 삭제
        c.execute("DELETE FROM products")
        
        # 필드 목록 정의 (순서 중요)
        fields = [
            'rank', 'goodsNo', 'linkUrl', 'goodsName', 'brandName', 'serial_number', 'imageUrl',
            'normalPrice', 'saleRate', 'salePrice', 'couponDiscount', 'finalPrice',
            'memberDiscountAmount', 'pointDiscountAmount',
            'availableSizes', 'soldout', 'baseCategoryFullName', 'memberDiscountRate',
            'memberSavePointRate', 'isMemberDiscount', 'isUsePointDiscount',
            'goodsSavePoint', 'couponName', 'couponValueKind', 'managedCode', 'goodsOptionNo',
            'lastObservedFinalPrice'
        ]
        
        # 데이터 준비
        rows_to_insert = []
        for p in products:
            product = dict(p)
            goods_no = str(product.get("goodsNo") or "")
            if product.get("lastObservedFinalPrice") is None:
                existing_last_price = existing_last_prices.get(goods_no)
                product["lastObservedFinalPrice"] = (
                    existing_last_price if existing_last_price is not None else product.get("finalPrice")
                )
            row = tuple(product.get(f) for f in fields)
            rows_to_insert.append(row)

        # 삽입 SQL
        placeholders = ",".join(["?"] * len(fields))
        sql = f"INSERT INTO products ({','.join(fields)}) VALUES ({placeholders})"
        
        c.executemany(sql, rows_to_insert)
        conn.commit()

def load_products() -> list[dict]:
    products = []
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM products ORDER BY rank ASC")
        rows = c.fetchall()
        for row in rows:
            products.append(dict(row))
    return products

def update_product_kream_id(goods_no: str, kream_product_id: str):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE products SET kream_product_id = ? WHERE goodsNo = ?", (kream_product_id, goods_no))
        conn.commit()

def get_kream_product_id(goods_no: str) -> str | None:
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT kream_product_id FROM products WHERE goodsNo = ?", (goods_no,))
        row = c.fetchone()
        return row["kream_product_id"] if row else None

def update_product_kream_url(goods_no: str, kream_product_url: str):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE products SET kream_product_url = ? WHERE goodsNo = ?", (kream_product_url, goods_no))
        conn.commit()

def update_product_snapshot(product: dict):
    fields = [
        'rank', 'linkUrl', 'goodsName', 'brandName', 'serial_number', 'imageUrl',
        'normalPrice', 'saleRate', 'salePrice', 'couponDiscount', 'finalPrice',
        'memberDiscountAmount', 'pointDiscountAmount',
        'availableSizes', 'soldout', 'baseCategoryFullName', 'memberDiscountRate',
        'memberSavePointRate', 'isMemberDiscount', 'isUsePointDiscount',
        'goodsSavePoint', 'couponName', 'couponValueKind', 'managedCode', 'goodsOptionNo',
        'lastObservedFinalPrice'
    ]
    goods_no = str(product.get("goodsNo") or "").strip()
    if not goods_no:
        return

    assignments = ", ".join(f"{field} = ?" for field in fields)
    values = [product.get(field) for field in fields]
    values.append(goods_no)

    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            f"UPDATE products SET {assignments}, crawled_at = CURRENT_TIMESTAMP WHERE goodsNo = ?",
            values,
        )
        conn.commit()

def update_last_observed_price(goods_no: str, last_observed_price: int | None):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE products SET lastObservedFinalPrice = ?, crawled_at = CURRENT_TIMESTAMP WHERE goodsNo = ?",
            (last_observed_price, goods_no),
        )
        conn.commit()

def get_kream_product_url(goods_no: str) -> str | None:
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT kream_product_url FROM products WHERE goodsNo = ?", (goods_no,))
        row = c.fetchone()
        return row["kream_product_url"] if row else None

def save_kream_size_prices(kream_product_id: str, prices_by_size: dict):
    with get_db_conn() as conn:
        c = conn.cursor()
        # 기존 데이터 삭제 (업데이트를 위해)
        c.execute("DELETE FROM kream_size_prices WHERE kream_product_id = ?", (kream_product_id,))
        
        rows_to_insert = []
        for size, price in prices_by_size.items():
            rows_to_insert.append((kream_product_id, size, price))

        c.executemany("INSERT INTO kream_size_prices (kream_product_id, size, price) VALUES (?, ?, ?)", rows_to_insert)
        conn.commit()
        
def save_kream_size_prices_v2(kream_product_id: str, prices_by_size: dict):
    with get_db_conn() as conn:
        c = conn.cursor()
        # 기존 데이터 삭제 (업데이트를 위해)
        c.execute("DELETE FROM kream_size_prices_v2 WHERE kream_product_id = ?", (kream_product_id,))
        
        rows_to_insert = []
        for size, price in prices_by_size.items():
            rows_to_insert.append((kream_product_id, size, price))

        c.executemany("INSERT INTO kream_size_prices_v2 (kream_product_id, size, price) VALUES (?, ?, ?)", rows_to_insert)
        conn.commit()

def load_kream_size_prices(kream_product_id: str) -> dict:
    prices = {}
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT size, price FROM kream_size_prices WHERE kream_product_id = ?", (kream_product_id,))
        rows = c.fetchall()
        for row in rows:
            prices[row["size"]] = row["price"]
    return prices

def load_kream_size_prices_v2(kream_product_id: str) -> dict:
    prices = {}
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT size, price FROM kream_size_prices_v2 WHERE kream_product_id = ?", (kream_product_id,))
        rows = c.fetchall()
        for row in rows:
            prices[row["size"]] = row["price"]
    return prices

def get_kream_product_ids_and_names() -> list[dict]:
    products_data = []
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT goodsName, kream_product_id FROM products WHERE kream_product_id IS NOT NULL")
        rows = c.fetchall()
        for row in rows:
            products_data.append({"goodsName": row["goodsName"], "kream_product_id": row["kream_product_id"]})
    return products_data

def update_product_kream_transactions(goods_no: str, total_transactions: int, monthly_transactions: int):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("""
            UPDATE products
            SET kream_total_transactions = ?,
                kream_monthly_transactions = ?
            WHERE goodsNo = ?
        """, (total_transactions, monthly_transactions, goods_no))
        conn.commit()

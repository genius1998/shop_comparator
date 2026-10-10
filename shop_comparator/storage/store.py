"""Additive persistence for the web app.

Uses the same SQLite file as the desktop GUI.  Only CREATE TABLE IF NOT EXISTS
and upserts are used: existing GUI tables and caches are never cleared here.
"""
import json
from datetime import datetime, timezone

from shop_comparator.storage import database

PRODUCT_FIELDS = [
    "rank", "linkUrl", "goodsName", "brandName", "serial_number", "imageUrl",
    "normalPrice", "saleRate", "salePrice", "couponDiscount", "finalPrice",
    "memberDiscountAmount", "pointDiscountAmount", "availableSizes", "soldout",
    "baseCategoryFullName", "memberDiscountRate", "memberSavePointRate",
    "isMemberDiscount", "isUsePointDiscount", "goodsSavePoint", "couponName",
    "couponValueKind", "managedCode", "goodsOptionNo",
]
VARIANT_FIELDS = [
    "size", "managedCode", "soldout", "remainQuantity", "optionPrice", "salePrice",
    "couponName", "couponValueKind", "couponDiscount", "memberDiscountRate",
    "memberDiscountAmount", "pointDiscountAmount", "finalPrice", "priceStatus",
    "priceSource", "error", "errorKind", "checkedAt", "stockCheckedAt",
]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init():
    database.init_db()
    with database.get_db_conn() as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS web_product_state (
            goodsNo TEXT PRIMARY KEY, active INTEGER DEFAULT 1, source TEXT, addedAt TEXT,
            musinsaStatus TEXT, musinsaError TEXT, musinsaCheckedAt TEXT,
            kreamStatus TEXT, kreamError TEXT, kreamCheckedAt TEXT,
            poizonStatus TEXT, poizonError TEXT, poizonCheckedAt TEXT, poizonLink TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS musinsa_variants (
            goodsNo TEXT NOT NULL, optionNo TEXT NOT NULL, size TEXT, managedCode TEXT,
            soldout INTEGER, remainQuantity INTEGER, optionPrice INTEGER, salePrice INTEGER,
            couponName TEXT, couponValueKind TEXT, couponDiscount INTEGER,
            memberDiscountRate REAL, memberDiscountAmount INTEGER, pointDiscountAmount INTEGER,
            finalPrice INTEGER, priceStatus TEXT, priceSource TEXT, error TEXT, errorKind TEXT,
            checkedAt TEXT, stockCheckedAt TEXT, present INTEGER DEFAULT 1,
            PRIMARY KEY (goodsNo, optionNo))""")
        c.execute("""CREATE TABLE IF NOT EXISTS kream_quotes (
            kreamId TEXT NOT NULL, size TEXT NOT NULL, instantBuy INTEGER, instantBuyDelivery TEXT,
            instantSell INTEGER, bidOnly INTEGER DEFAULT 0, candidates TEXT, checkedAt TEXT,
            present INTEGER DEFAULT 1, PRIMARY KEY (kreamId, size))""")
        c.execute("""CREATE TABLE IF NOT EXISTS poizon_quotes (
            goodsNo TEXT NOT NULL, size TEXT NOT NULL, price INTEGER, checkedAt TEXT,
            PRIMARY KEY (goodsNo, size))""")
        c.execute("""CREATE TABLE IF NOT EXISTS job_runs (
            id TEXT PRIMARY KEY, kind TEXT, title TEXT, state TEXT, startedAt TEXT,
            finishedAt TEXT, summary TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS monitor_items (
            kind TEXT NOT NULL, goodsNo TEXT NOT NULL, brandName TEXT, goodsName TEXT,
            status TEXT, state TEXT, updatedAt TEXT, PRIMARY KEY (kind, goodsNo))""")
        c.execute("CREATE TABLE IF NOT EXISTS web_settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("""CREATE TABLE IF NOT EXISTS site_sessions (
            site TEXT PRIMARY KEY, state TEXT, account TEXT, verifiedAt TEXT, detail TEXT)""")
        # AI suggestion and human decision are separate records: a suggestion never changes a match.
        c.execute("""CREATE TABLE IF NOT EXISTS vlm_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, goodsNo TEXT NOT NULL, model TEXT, source TEXT,
            status TEXT NOT NULL, decision TEXT, kreamId TEXT, confidence REAL, reason TEXT,
            candidateKey TEXT, candidateIds TEXT, errorKind TEXT, error TEXT,
            inputTokens INTEGER, outputTokens INTEGER, seconds REAL, jobId TEXT, judgedAt TEXT, createdAt TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS vlm_suggestions_goods ON vlm_suggestions (goodsNo, id)")
        c.execute("""CREATE TABLE IF NOT EXISTS match_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT, goodsNo TEXT NOT NULL, action TEXT NOT NULL, kreamId TEXT,
            note TEXT, suggestionId INTEGER, suggestionDecision TEXT, suggestionKreamId TEXT,
            previousKreamId TEXT, previousMethod TEXT, mode TEXT, reviewSeconds REAL, createdAt TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS match_reviews_goods ON match_reviews (goodsNo, id)")
        # One row per target of a job stage, so an interrupted run can be finished without redoing what succeeded.
        c.execute("""CREATE TABLE IF NOT EXISTS job_items (
            jobId TEXT NOT NULL, stage TEXT NOT NULL, goodsNo TEXT NOT NULL, state TEXT NOT NULL,
            errorKind TEXT, error TEXT, updatedAt TEXT, PRIMARY KEY (jobId, stage, goodsNo))""")
        conn.commit()


def _rows(sql, args=()):
    with database.get_db_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _exec(sql, args=()):
    with database.get_db_conn() as conn:
        conn.execute(sql, args)
        conn.commit()


# ---------- products ----------
def upsert_product(product, source=None):
    """Insert or update one Musinsa product without touching other rows.

    KREAM ids, transaction counts and the last observed price survive.
    """
    goods_no = str(product.get("goodsNo") or "").strip()
    if not goods_no:
        return
    fields = [f for f in PRODUCT_FIELDS if f in product]
    with database.get_db_conn() as conn:
        c = conn.cursor()
        exists = c.execute("SELECT lastObservedFinalPrice FROM products WHERE goodsNo = ?", (goods_no,)).fetchone()
        if exists:
            if fields:
                assignments = ", ".join(f"{f} = ?" for f in fields)
                c.execute(f"UPDATE products SET {assignments}, crawled_at = CURRENT_TIMESTAMP WHERE goodsNo = ?",
                          [product.get(f) for f in fields] + [goods_no])
            if exists["lastObservedFinalPrice"] is None and product.get("finalPrice") is not None:
                c.execute("UPDATE products SET lastObservedFinalPrice = ? WHERE goodsNo = ?", (product["finalPrice"], goods_no))
        else:
            cols = ["goodsNo"] + fields + ["lastObservedFinalPrice"]
            values = [goods_no] + [product.get(f) for f in fields] + [product.get("finalPrice")]
            c.execute(f"INSERT INTO products ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", values)
        c.execute("""INSERT INTO web_product_state (goodsNo, active, source, addedAt) VALUES (?, 1, ?, ?)
                     ON CONFLICT(goodsNo) DO UPDATE SET active = 1, source = COALESCE(excluded.source, source)""",
                  (goods_no, source, now()))
        conn.commit()


def set_state(goods_no, shop, status, error=None):
    column = {"musinsa": "musinsa", "kream": "kream", "poizon": "poizon"}[shop]
    with database.get_db_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO web_product_state (goodsNo, active, addedAt) VALUES (?, 1, ?)", (str(goods_no), now()))
        conn.execute(f"UPDATE web_product_state SET {column}Status = ?, {column}Error = ?, {column}CheckedAt = ? WHERE goodsNo = ?",
                     (status, error, now(), str(goods_no)))
        conn.commit()


def set_active(goods_nos, active):
    with database.get_db_conn() as conn:
        for goods_no in goods_nos:
            conn.execute("INSERT OR IGNORE INTO web_product_state (goodsNo, active, addedAt) VALUES (?, 1, ?)", (str(goods_no), now()))
            conn.execute("UPDATE web_product_state SET active = ? WHERE goodsNo = ?", (1 if active else 0, str(goods_no)))
        conn.commit()


def products(active_only=True, goods_nos=None):
    """Products in the web list. GUI-era rows without web state count as active."""
    sql = """SELECT p.*, COALESCE(s.active, 1) AS active, s.source, s.musinsaStatus, s.musinsaError,
                    s.musinsaCheckedAt, s.kreamStatus, s.kreamError, s.kreamCheckedAt,
                    s.poizonStatus, s.poizonError, s.poizonCheckedAt, s.poizonLink
             FROM products p LEFT JOIN web_product_state s ON s.goodsNo = p.goodsNo"""
    rows = _rows(sql + " ORDER BY CASE WHEN p.rank IS NULL OR p.rank = '' THEN 1 ELSE 0 END, p.rank, p.id")
    if active_only:
        rows = [r for r in rows if r["active"]]
    if goods_nos is not None:
        wanted = {str(g) for g in goods_nos}
        rows = [r for r in rows if str(r["goodsNo"]) in wanted]
    return rows


def product(goods_no):
    found = products(active_only=False, goods_nos=[goods_no])
    return found[0] if found else None


# ---------- Musinsa variants ----------
def save_variant(goods_no, option_no, values, price_attempted=True):
    """Upsert one size. A failed price lookup keeps the last verified price
    and records the error next to it instead of overwriting it."""
    goods_no, option_no = str(goods_no), str(option_no)
    with database.get_db_conn() as conn:
        c = conn.cursor()
        old = c.execute("SELECT * FROM musinsa_variants WHERE goodsNo = ? AND optionNo = ?", (goods_no, option_no)).fetchone()
        row = dict(old) if old else {"goodsNo": goods_no, "optionNo": option_no}
        failed = price_attempted and values.get("priceStatus") == "error"
        for key in VARIANT_FIELDS:
            if key not in values:
                continue
            if failed and key in {"salePrice", "couponName", "couponValueKind", "couponDiscount", "memberDiscountRate",
                                  "memberDiscountAmount", "pointDiscountAmount", "finalPrice", "priceSource", "checkedAt"}:
                continue  # keep the last verified numbers and their timestamp
            row[key] = values[key]
        if failed and old and old["finalPrice"] is not None:
            row["priceStatus"] = "stale"
        row["present"] = 1
        cols = ["goodsNo", "optionNo"] + VARIANT_FIELDS + ["present"]
        c.execute(f"INSERT OR REPLACE INTO musinsa_variants ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                  [row.get(col) for col in cols])
        conn.commit()


def mark_missing_variants(goods_no, present_option_nos):
    """Sizes that no longer exist on the product page are flagged, not deleted."""
    keep = {str(n) for n in present_option_nos}
    with database.get_db_conn() as conn:
        for row in conn.execute("SELECT optionNo FROM musinsa_variants WHERE goodsNo = ?", (str(goods_no),)).fetchall():
            if row["optionNo"] not in keep:
                conn.execute("UPDATE musinsa_variants SET present = 0 WHERE goodsNo = ? AND optionNo = ?", (str(goods_no), row["optionNo"]))
        conn.commit()


def variants(goods_no=None):
    if goods_no is None:
        return _rows("SELECT * FROM musinsa_variants WHERE present = 1")
    return _rows("SELECT * FROM musinsa_variants WHERE goodsNo = ? AND present = 1", (str(goods_no),))


# ---------- KREAM ----------
def save_kream_quotes(kream_id, quotes):
    """Replace the quote set of one KREAM product after a successful fetch."""
    kream_id = str(kream_id)
    stamp = now()
    with database.get_db_conn() as conn:
        conn.execute("UPDATE kream_quotes SET present = 0 WHERE kreamId = ?", (kream_id,))
        for q in quotes:
            conn.execute("""INSERT OR REPLACE INTO kream_quotes
                (kreamId, size, instantBuy, instantBuyDelivery, instantSell, bidOnly, candidates, checkedAt, present)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (kream_id, str(q["size"]), q.get("instantBuy"), q.get("instantBuyDelivery"), q.get("instantSell"),
                 1 if q.get("bidOnly") else 0, json.dumps(q.get("candidates") or [], ensure_ascii=False), stamp))
        conn.commit()


def kream_quotes(kream_id=None):
    if kream_id is None:
        return _rows("SELECT * FROM kream_quotes WHERE present = 1")
    return _rows("SELECT * FROM kream_quotes WHERE kreamId = ? AND present = 1", (str(kream_id),))


def match_for(goods_no):
    return database.get_cached_kream_search(str(goods_no))


MATCH_COLUMNS = ("musinsa_goods_no, kream_product_id, kream_product_url, kream_brand, kream_product_name, "
                 "match_verified, match_method, match_confidence, match_reason, verified_at")


def all_matches():
    # kream_all_text / image columns are large and not needed for display.
    return {r["musinsa_goods_no"]: r for r in _rows(f"SELECT {MATCH_COLUMNS} FROM kream_search_cache")}


def counts():
    """Cheap numbers for the status poll (no row loading)."""
    row = _rows("""
        SELECT
          (SELECT COUNT(*) FROM products p LEFT JOIN web_product_state s ON s.goodsNo = p.goodsNo
            WHERE COALESCE(s.active, 1) = 1) AS products,
          (SELECT COUNT(*) FROM musinsa_variants WHERE present = 1) AS variants,
          (SELECT COUNT(*) FROM products p LEFT JOIN web_product_state s ON s.goodsNo = p.goodsNo
            JOIN kream_search_cache m ON m.musinsa_goods_no = p.goodsNo
            WHERE COALESCE(s.active, 1) = 1 AND COALESCE(m.match_verified, 0) = 1
              AND COALESCE(m.kream_product_id, '') NOT IN ('', '0')) AS matched""")[0]
    return row


def monitor_count(kind):
    return _rows("SELECT COUNT(*) AS n FROM monitor_items WHERE kind = ?", (kind,))[0]["n"]


def set_manual_match(goods_no, kream_id, note="사용자 직접 지정", method="manual", confidence=1.0):
    """Register a match decided outside the automatic verifier.

    method: 'manual' (user typed the KREAM URL) or 'image_review'
    (thumbnails compared side by side; see match_review/verdicts.csv).
    """
    goods_no, kream_id = str(goods_no), str(kream_id)
    database.init_db()
    with database.get_db_conn() as conn:
        conn.execute("""INSERT INTO kream_search_cache
            (musinsa_goods_no, kream_product_id, kream_product_url, match_verified, match_method,
             match_confidence, match_reason, verified_at, last_searched_at)
            VALUES (?, ?, ?, 1, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(musinsa_goods_no) DO UPDATE SET
              kream_product_id = excluded.kream_product_id, kream_product_url = excluded.kream_product_url,
              kream_brand = NULL, kream_product_name = NULL, kream_image_url = NULL, kream_all_text = NULL,
              match_verified = 1, match_method = excluded.match_method, match_confidence = excluded.match_confidence,
              match_reason = excluded.match_reason, verified_at = CURRENT_TIMESTAMP,
              last_searched_at = CURRENT_TIMESTAMP""",
            (goods_no, kream_id, f"https://kream.co.kr/products/{kream_id}", method, confidence, note))
        conn.execute("UPDATE products SET kream_product_id = ?, kream_product_url = ? WHERE goodsNo = ?",
                     (kream_id, f"https://kream.co.kr/products/{kream_id}", goods_no))
        conn.commit()


def approve_match(goods_no):
    _exec("""UPDATE kream_search_cache SET match_verified = 1, match_method = COALESCE(match_method, 'approved'),
             match_reason = COALESCE(match_reason, '') || ' / 사용자 승인', verified_at = CURRENT_TIMESTAMP
             WHERE musinsa_goods_no = ?""", (str(goods_no),))


def clear_match(goods_no):
    """User-requested unmatch of a single product."""
    with database.get_db_conn() as conn:
        conn.execute("DELETE FROM kream_search_cache WHERE musinsa_goods_no = ?", (str(goods_no),))
        conn.execute("UPDATE products SET kream_product_id = NULL, kream_product_url = NULL WHERE goodsNo = ?", (str(goods_no),))
        conn.commit()


# ---------- POIZON ----------
def save_poizon_quotes(goods_no, prices, link=None):
    stamp = now()
    with database.get_db_conn() as conn:
        conn.execute("DELETE FROM poizon_quotes WHERE goodsNo = ?", (str(goods_no),))
        for size, price in prices.items():
            conn.execute("INSERT OR REPLACE INTO poizon_quotes (goodsNo, size, price, checkedAt) VALUES (?, ?, ?, ?)",
                         (str(goods_no), str(size), price, stamp))
        conn.execute("INSERT OR IGNORE INTO web_product_state (goodsNo, active, addedAt) VALUES (?, 1, ?)", (str(goods_no), stamp))
        if link:
            conn.execute("UPDATE web_product_state SET poizonLink = ? WHERE goodsNo = ?", (link, str(goods_no)))
        conn.commit()


def poizon_quotes(goods_no=None):
    if goods_no is None:
        return _rows("SELECT * FROM poizon_quotes")
    return _rows("SELECT * FROM poizon_quotes WHERE goodsNo = ?", (str(goods_no),))


# ---------- settings / sessions / jobs / monitors ----------
def get_setting(key, default=None):
    rows = _rows("SELECT value FROM web_settings WHERE key = ?", (key,))
    if not rows:
        return default
    try:
        return json.loads(rows[0]["value"])
    except ValueError:
        return default


def set_setting(key, value):
    _exec("INSERT OR REPLACE INTO web_settings (key, value) VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))


def save_session(site, state, account=None, detail=None, verified=False):
    with database.get_db_conn() as conn:
        old = conn.execute("SELECT * FROM site_sessions WHERE site = ?", (site,)).fetchone()
        account = account if account is not None else (old["account"] if old else None)
        verified_at = now() if verified else (old["verifiedAt"] if old else None)
        conn.execute("INSERT OR REPLACE INTO site_sessions (site, state, account, verifiedAt, detail) VALUES (?, ?, ?, ?, ?)",
                     (site, state, account, verified_at, detail))
        conn.commit()


def sessions():
    return {r["site"]: r for r in _rows("SELECT * FROM site_sessions")}


def save_job(job):
    _exec("INSERT OR REPLACE INTO job_runs (id, kind, title, state, startedAt, finishedAt, summary) VALUES (?, ?, ?, ?, ?, ?, ?)",
          (job["id"], job["kind"], job["title"], job["state"], job["startedAt"], job.get("finishedAt"),
           json.dumps(job.get("summary") or {}, ensure_ascii=False)))


def recent_jobs(limit=30):
    rows = _rows("SELECT * FROM job_runs ORDER BY startedAt DESC LIMIT ?", (limit,))
    for row in rows:
        try:
            row["summary"] = json.loads(row["summary"] or "{}")
        except ValueError:
            row["summary"] = {}
    return rows


def interrupt_stale_jobs():
    """Jobs still marked running belong to a server process that has exited."""
    _exec("UPDATE job_runs SET state = 'interrupted', finishedAt = ? WHERE state IN ('running', 'stopping')", (now(),))


def monitor_items(kind):
    rows = _rows("SELECT * FROM monitor_items WHERE kind = ? ORDER BY updatedAt", (kind,))
    for row in rows:
        try:
            row["state"] = json.loads(row["state"] or "{}")
        except ValueError:
            row["state"] = {}
    return rows


def save_monitor_item(kind, goods_no, brand, name, status, state):
    _exec("INSERT OR REPLACE INTO monitor_items (kind, goodsNo, brandName, goodsName, status, state, updatedAt) VALUES (?, ?, ?, ?, ?, ?, ?)",
          (kind, str(goods_no), brand, name, status, json.dumps(state, ensure_ascii=False), now()))


def delete_monitor_items(kind, goods_nos):
    with database.get_db_conn() as conn:
        for goods_no in goods_nos:
            conn.execute("DELETE FROM monitor_items WHERE kind = ? AND goodsNo = ?", (kind, str(goods_no)))
        conn.commit()


# ---------- job items (what a job was asked to do, and how far it got) ----------
def plan_items(job_id, stage, goods_nos):
    """Register the targets of a stage as 'pending'.  Re-planning keeps finished rows."""
    with database.get_db_conn() as conn:
        conn.executemany("INSERT OR IGNORE INTO job_items (jobId, stage, goodsNo, state, updatedAt) VALUES (?, ?, ?, 'pending', ?)",
                         [(job_id, stage, str(g), now()) for g in goods_nos])
        conn.commit()


def mark_item(job_id, stage, goods_no, state, error_kind=None, error=None):
    """state: ok | failed | skipped.  One row per (job, stage, product); a retry overwrites, never duplicates."""
    _exec("""INSERT INTO job_items (jobId, stage, goodsNo, state, errorKind, error, updatedAt) VALUES (?, ?, ?, ?, ?, ?, ?)
             ON CONFLICT(jobId, stage, goodsNo) DO UPDATE SET state = excluded.state, errorKind = excluded.errorKind,
               error = excluded.error, updatedAt = excluded.updatedAt""",
          (job_id, stage, str(goods_no), state, error_kind, (error or "")[:300] or None, now()))


def job_items(job_id):
    return _rows("SELECT * FROM job_items WHERE jobId = ? ORDER BY rowid", (job_id,))


def job_item_summary(job_id):
    """{stage: {pending, ok, failed, skipped}} for one job."""
    out = {}
    for row in _rows("SELECT stage, state, COUNT(*) AS n FROM job_items WHERE jobId = ? GROUP BY stage, state", (job_id,)):
        out.setdefault(row["stage"], {"pending": 0, "ok": 0, "failed": 0, "skipped": 0})[row["state"]] = row["n"]
    return out


def job_run(job_id):
    rows = _rows("SELECT * FROM job_runs WHERE id = ?", (job_id,))
    if not rows:
        return None
    row = rows[0]
    try:
        row["summary"] = json.loads(row.get("summary") or "{}")
    except ValueError:
        row["summary"] = {}
    return row


# ---------- VLM suggestions and human reviews ----------
SUGGESTION_FIELDS = ("goodsNo", "model", "source", "status", "decision", "kreamId", "confidence", "reason", "candidateKey",
                     "candidateIds", "errorKind", "error", "inputTokens", "outputTokens", "seconds", "jobId", "judgedAt")


def add_suggestion(**values):
    """Append one model verdict (or one failed attempt).  Earlier rows are kept as history."""
    row = {k: values.get(k) for k in SUGGESTION_FIELDS}
    row["createdAt"] = now()
    with database.get_db_conn() as conn:
        cursor = conn.execute(f"INSERT INTO vlm_suggestions ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
        conn.commit()
        return cursor.lastrowid


def latest_suggestions(goods_nos=None, valid_only=False):
    """{goodsNo: newest suggestion row}.  valid_only skips failed attempts so an error never hides a good verdict."""
    where = "WHERE status = 'valid'" if valid_only else ""
    rows = _rows(f"SELECT * FROM vlm_suggestions WHERE id IN (SELECT MAX(id) FROM vlm_suggestions {where} GROUP BY goodsNo)")
    wanted = None if goods_nos is None else {str(g) for g in goods_nos}
    return {r["goodsNo"]: r for r in rows if wanted is None or r["goodsNo"] in wanted}


def suggestion_counts(goods_nos=None):
    rows = _rows("SELECT goodsNo, status, COUNT(*) AS n FROM vlm_suggestions GROUP BY goodsNo, status")
    wanted = None if goods_nos is None else {str(g) for g in goods_nos}
    out = {}
    for row in rows:
        if wanted is None or row["goodsNo"] in wanted:
            out.setdefault(row["goodsNo"], {})[row["status"]] = row["n"]
    return out


def add_review(goods_no, action, kream_id=None, note=None, suggestion=None, previous=None, mode=None, seconds=None):
    suggestion, previous = suggestion or {}, previous or {}
    with database.get_db_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO match_reviews (goodsNo, action, kreamId, note, suggestionId, suggestionDecision, suggestionKreamId,
                                          previousKreamId, previousMethod, mode, reviewSeconds, createdAt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(goods_no), action, kream_id, (note or "")[:300] or None, suggestion.get("id"), suggestion.get("decision"),
             suggestion.get("kreamId"), previous.get("kreamId") or None, previous.get("method") or None, mode, seconds, now()))
        conn.commit()
        return cursor.lastrowid


def reviews(goods_nos=None):
    rows = _rows("SELECT * FROM match_reviews ORDER BY id")
    wanted = None if goods_nos is None else {str(g) for g in goods_nos}
    return [r for r in rows if wanted is None or r["goodsNo"] in wanted]


def latest_reviews(goods_nos=None):
    return {r["goodsNo"]: r for r in reviews(goods_nos)}

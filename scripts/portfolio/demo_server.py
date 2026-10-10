"""Run the web app on the demo folder with replay adapters.  Used only for portfolio captures.

    python scripts/portfolio/demo_server.py [--dir portfolio_demo] [--port 8799] [--fail GOODS,GOODS,...]

* The working directory is the demo folder, so the app opens the demo database.
* .env is not read: no account, no API key.  Login functions are replaced by refusals.
* KREAM price collection replays the quotes already stored in the demo database
  (no request is sent).  --fail makes the listed products time out on their first
  two attempts: this is TEST FAULT INJECTION, used to show stop and resume.
* Stored VLM verdicts are replayed with a short pause per product so the
  progress bar can be seen.
"""
import argparse
import os
import sys
import time
from contextlib import contextmanager
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LABEL = "데모 · 저장된 실데이터 재생 · 네트워크 미호출 (2026-10-09 수집분, 운영 DB와 분리된 사본)"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=os.path.join(ROOT, "portfolio_demo"))
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--fail", default="")
    parser.add_argument("--delay", type=float, default=0.25)
    args = parser.parse_args()
    os.chdir(args.dir)
    sys.path.insert(0, ROOT)
    for key in list(os.environ):
        if key.startswith(("MUSINSA_", "KREAM_", "POIZON_", "OPENAI_")):
            del os.environ[key]
    os.environ["SHOP_SKIP_DOTENV"] = "1"
    os.environ["SHOP_AUTOMATION"] = "1"          # whatever a script clicks here is recorded as automation, never as a person
    os.environ["SHOP_DEMO_LABEL"] = LABEL

    from shop_comparator.core.crawl_errors import CrawlError
    from shop_comparator.services import pipeline, vlm_review
    from shop_comparator.shops import site_login
    from shop_comparator.storage import store
    from shop_comparator.web import web_app

    failing, attempts = {g for g in args.fail.split(",") if g}, {}

    @contextmanager
    def no_browser(*a, **k):
        yield mock.Mock(name="replay-session"), None

    def replay_quotes(kream_id, token=None):
        """Stored quotes of the demo database instead of a request to KREAM."""
        goods = next((str(p["goodsNo"]) for p in store.products(active_only=False) if str(p.get("kream_product_id") or "").split(".")[0] == str(kream_id)), "")
        attempts[goods] = attempts.get(goods, 0) + 1
        if goods in failing and attempts[goods] <= 2:
            raise CrawlError("KREAM", "network", "연결 시간 초과 (테스트 장애 주입)")
        rows = store.kream_quotes(kream_id)
        if not rows:
            raise CrawlError("KREAM", "schema", "데모 DB에 저장된 시세가 없습니다.")
        return [{"size": r["size"], "instantBuy": r["instantBuy"], "instantBuyDelivery": r["instantBuyDelivery"],
                 "instantSell": r["instantSell"], "bidOnly": bool(r["bidOnly"]), "candidates": []} for r in rows]

    def refuse(*a, **k):
        raise CrawlError("데모", "prerequisite", "데모에서는 쇼핑몰에 로그인하거나 접속하지 않습니다.")

    real_validate = vlm_review.validate

    def paced_validate(entry, answer):
        time.sleep(args.delay)
        return real_validate(entry, answer)

    patches = [
        mock.patch.object(pipeline, "kream_session", no_browser),
        mock.patch.object(pipeline, "musinsa_session", refuse),
        mock.patch.object(pipeline, "match_products", lambda products, ctx, force=False: None),
        mock.patch.object(pipeline, "_notify_margin", lambda *a, **k: None),
        mock.patch.object(pipeline.kream_api, "fetch_quotes", replay_quotes),
        mock.patch.object(site_login, "login_musinsa", refuse),
        mock.patch.object(site_login, "login_kream", refuse),
        mock.patch.object(vlm_review, "validate", paced_validate),
        mock.patch.object(pipeline.database, "save_kream_size_prices", lambda *a, **k: None),
        mock.patch.object(pipeline.database, "save_kream_size_prices_v2", lambda *a, **k: None),
    ]
    for patch in patches:
        patch.start()
    # shorter pauses between replayed products (nothing is being rate limited here)
    from shop_comparator.services.jobs import JobContext
    original_wait = JobContext.wait
    JobContext.wait = lambda self, seconds: original_wait(self, min(seconds, 0.4))

    sys.argv = [sys.argv[0], "--port", str(args.port)]
    web_app.main()


if __name__ == "__main__":
    main()

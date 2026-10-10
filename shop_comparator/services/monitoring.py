"""Ports of the GUI monitoring windows and the event-trigger automation.

Three monitors (재입고 / 가격변동 / 구매제한) keep their item lists in the
database, so the lists survive a restart.  Loops themselves are not resumed
automatically after a restart; the user starts them again.

Each cycle borrows the Musinsa Chrome profile, so a cycle waits while a
collection job is using it and is skipped (and logged) if the wait is long.
"""
import threading
import time
from datetime import datetime

from shop_comparator.core import app_logging as L
from shop_comparator.services import comparison_service as cmp
from shop_comparator.shops import crawler
from shop_comparator.storage import database
from shop_comparator.services import pipeline
from shop_comparator.storage import store
from shop_comparator.core import webhook
from shop_comparator.shops.browser_transport import browser_transport
from shop_comparator.core.crawl_errors import CrawlError

KINDS = {
    "restock": {"label": "재입고", "interval": 180, "tag": "Monitor"},
    "price": {"label": "가격변동", "interval": 300, "tag": "PriceMon"},
    "limit": {"label": "구매제한", "interval": 240, "tag": "LimitMon"},
}
LOCK_WAIT = 120


def _sorted(values):
    return sorted({str(v) for v in values if str(v).strip()}, key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else 0, x))


def stock_text(stock):
    parts = []
    if stock.get("available"):
        parts.append("재고있음: " + ", ".join(_sorted(stock["available"])))
    if stock.get("unavailable"):
        parts.append("품절: " + ", ".join(_sorted(stock["unavailable"])))
    return " / ".join(parts) or "정보 없음"


def _notify(message):
    url = store.get_setting("webhook_url")
    if url:
        sent = webhook.send_discord_notification(url, message)
        L.log("디스코드 알림 " + ("전송" if sent else "전송 실패"), stage="notify", level="info" if sent else "warning")


# -------------------------------------------------- single-item checks
def restock_check(session, goods_no, previous):
    """Returns (status, new_state, flag, message-or-None). Mirrors _patched_monitoring_loop."""
    stock = crawler.extract_stock_size_sets(crawler.fetch_options_any(session, goods_no))
    available, unavailable = set(stock["available"]), set(stock["unavailable"])
    last = set(previous.get("available") or [])
    if available and unavailable:
        status = "일부 재고 있음"
    elif available:
        status = "재고 있음"
    elif unavailable:
        status = "품절"
    else:
        status = "정보 없음"
    flag, message = "", None
    if previous.get("initialised") and not last and available:
        status, flag = "재입고", "good"
        message = "restock"
    elif last and not available:
        status, flag = "품절", "muted"
    elif previous.get("initialised") and available - last:
        status, flag = "새 입고: " + ", ".join(_sorted(available - last)), "good"
        message = "restock"
    state = {"initialised": True, "available": _sorted(available), "unavailable": _sorted(unavailable), "text": stock_text(stock)}
    return status, state, flag, message


def price_check(session, payload, goods_no, previous):
    """Current member price of the first purchasable option vs. the first price seen."""
    option = crawler.choose_sellable_option(crawler.fetch_options_any(session, goods_no))
    original = previous.get("original")
    if option is None:
        return "확인 불가", {**previous, "current": None}, "", None
    current = crawler.price_option("", goods_no, option[1], auth_payload=payload, sleep_each=0.3)["finalPrice"]
    if original is None:
        return "모니터링 시작", {"original": current, "current": current, "size": option[0]}, "", None
    if current < original:
        return "가격 변동 감지", {**previous, "current": current, "size": option[0]}, "down", "price"
    return "변동 없음", {**previous, "current": current, "size": option[0]}, "", None


def limit_check(session, goods_no, previous):
    info = crawler.fetch_purchase_limit_info(session, goods_no)
    if not info:
        return "정보 없음", previous, "", None, None
    limited = bool(info.get("isLimitedQuantity"))
    was = previous.get("limited")
    state = {"limited": limited, "initialised": True}
    if was and not limited:
        return "구매제한 해제!", state, "good", "limit", info
    return ("구매 제한" if limited else "제한 없음"), state, ("warn" if limited else ""), None, info


class Monitor:
    def __init__(self, kind):
        self.kind = kind
        self.meta = KINDS[kind]
        self.active = threading.Event()
        self.thread = None
        self.interval = self.meta["interval"]
        self.last_cycle = None
        self.lock = threading.Lock()

    # ----- items
    def items(self):
        return store.monitor_items(self.kind)

    def add(self, goods_nos, from_list=False):
        """Add products; details/baseline are read from Musinsa (needs the browser)."""
        existing = {i["goodsNo"] for i in self.items()}
        wanted = [str(g) for g in goods_nos if str(g) not in existing]
        if not wanted:
            return 0
        known = {str(p["goodsNo"]): p for p in store.products(active_only=False, goods_nos=wanted)}
        added = 0
        with L.bind(shop="musinsa", stage=f"monitor-{self.kind}"):
            with pipeline.musinsa_session(need_login=self.kind == "price") as (session, payload):
                details = crawler.fetch_details(session, [g for g in wanted if g not in known], sleep=0.2) if any(g not in known for g in wanted) else {}
                for goods_no in wanted:
                    meta = known.get(goods_no) or details.get(goods_no)
                    if not meta:
                        L.warn(f"상품 {goods_no} 정보를 찾을 수 없어 추가하지 않았습니다.", goods=goods_no)
                        continue
                    try:
                        status, state = self._baseline(session, payload, goods_no)
                    except CrawlError as exc:
                        if exc.code in pipeline.STOP_CODES:
                            raise
                        status, state = "오류", {"error": str(exc)}
                    store.save_monitor_item(self.kind, goods_no, meta.get("brandName") or "", meta.get("goodsName") or "", status, state)
                    added += 1
                    L.log(f"{self.meta['label']} 모니터링 상품 추가: {status}", goods=goods_no)
                    time.sleep(0.3)
        return added

    def _baseline(self, session, payload, goods_no):
        if self.kind == "restock":
            _, state, _, _ = restock_check(session, goods_no, {})
            return ("모니터링 시작" if state["available"] else "품절 상태로 시작"), state
        if self.kind == "price":
            status, state, _, _ = price_check(session, payload, goods_no, {})
            return status, state
        status, state, _, _, _ = limit_check(session, goods_no, {})
        return status, state

    def remove(self, goods_nos):
        store.delete_monitor_items(self.kind, goods_nos)

    # ----- loop
    def start(self, interval):
        if interval <= 0:
            raise ValueError("모니터링 주기는 1초 이상의 정수여야 합니다.")
        if not self.items():
            raise ValueError("모니터링할 상품을 먼저 추가해 주세요.")
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("이미 모니터링이 실행 중입니다.")
            self.interval = interval
            self.active.set()
            self.thread = threading.Thread(target=self._loop, daemon=True, name=f"monitor-{self.kind}")
            self.thread.start()

    def stop(self):
        self.active.clear()

    def running(self):
        return bool(self.thread and self.thread.is_alive() and self.active.is_set())

    def _loop(self):
        with L.bind(job=f"monitor-{self.kind}", shop="musinsa", stage=f"monitor-{self.kind}"):
            L.log(f"{self.meta['label']} 모니터링 시작 (주기 {self.interval}초)")
            while self.active.is_set():
                try:
                    self.cycle()
                except CrawlError as exc:
                    L.error(f"모니터링 주기 실패: {exc}", exc.code)
                    if exc.code == "auth":
                        L.warn("무신사 로그인이 필요해 모니터링을 중지합니다.")
                        self.active.clear()
                        break
                except Exception as exc:
                    L.error(f"모니터링 주기 실패: {type(exc).__name__}", "internal")
                self.last_cycle = store.now()
                end = time.monotonic() + self.interval
                while self.active.is_set() and time.monotonic() < end:
                    time.sleep(1)
            L.log(f"{self.meta['label']} 모니터링 종료")

    def cycle(self):
        items = self.items()
        if not items:
            return
        from shop_comparator.shops import site_login
        with browser_transport("musinsa", cookies=site_login.musinsa_saved_cookies(), wait=LOCK_WAIT) as browser:
            payload = {"cookies": browser.cookies(), "user_agent": browser.driver.execute_script("return navigator.userAgent")}
            crawler.set_auth_payload(payload)
            session = crawler.build_session("", payload)
            for item in items:
                if not self.active.is_set():
                    break
                goods_no = item["goodsNo"]
                with L.bind(goods=goods_no):
                    try:
                        info = None
                        if self.kind == "restock":
                            status, state, flag, event = restock_check(session, goods_no, item["state"])
                        elif self.kind == "price":
                            status, state, flag, event = price_check(session, payload, goods_no, item["state"])
                        else:
                            status, state, flag, event, info = limit_check(session, goods_no, item["state"])
                        state["flag"] = flag
                        store.save_monitor_item(self.kind, goods_no, item["brandName"], item["goodsName"], status, state)
                        if event:
                            L.log(f"{self.meta['label']} 감지: {status}")
                            _notify(self._message(event, item, state))
                    except CrawlError as exc:
                        store.save_monitor_item(self.kind, goods_no, item["brandName"], item["goodsName"], "오류",
                                                {**item["state"], "flag": "bad", "error": str(exc)})
                        if exc.code in pipeline.STOP_CODES:
                            raise
                        L.error(f"상품 확인 실패: {exc}", exc.code)
                time.sleep(2)

    def _message(self, event, item, state):
        link = f"[상품 바로가기](https://www.musinsa.com/products/{item['goodsNo']})"
        head = f"**상품 ID:** {item['goodsNo']}\n**브랜드:** {item['brandName']}\n**상품명:** {item['goodsName']}\n"
        if event == "restock":
            return f"**무신사 재입고 알림**\n\n{head}**재고 상태:** {state.get('text')}\n{link}"
        if event == "price":
            original, current = state.get("original"), state.get("current")
            return (f"**🚨 가격 변동 알림: 가격 변동 감지 🚨**\n\n{head}**최초가:** {original:,}원\n"
                    f"**현재가:** {current:,}원 (▼ {original - current:,}원)\n{link}")
        return f"**🔓 구매제한 해제 알림 🔓**\n\n{head}{link}"

    def snapshot(self):
        return {"kind": self.kind, "label": self.meta["label"], "running": self.running(), "interval": self.interval,
                "defaultInterval": self.meta["interval"], "lastCycle": self.last_cycle, "items": self.items()}


class AutoEvent:
    """GUI "이벤트 트리거 자동화": day = watch Musinsa prices and compare with
    KREAM on a drop; night (00-09h) = refresh everything."""

    def __init__(self, jobs=None):
        self.active = threading.Event()
        self.thread = None
        self.status = "대기"
        self.mode_override = None
        self.day_interval = 300
        self.night_interval = 1800
        self.seed = None
        self.last_prices = {}
        self.cycles = 0

    @staticmethod
    def interval(value, default):
        """GUI rule: minimum 30 seconds; invalid input falls back to the default."""
        try:
            return max(30, int(float(str(value).strip() or default)))
        except (TypeError, ValueError):
            return default

    def mode(self):
        if self.mode_override in ("night_full", "day_event"):
            return self.mode_override
        return "night_full" if 0 <= datetime.now().hour < 9 else "day_event"

    def start(self, mode, day, night, seed=None):
        if self.thread and self.thread.is_alive():
            raise ValueError("자동 이벤트 모드가 이미 실행 중입니다.")
        self.mode_override = mode if mode in ("night_full", "day_event") else None
        self.day_interval = self.interval(day, 300)
        self.night_interval = self.interval(night, 1800)
        self.seed = seed
        self.active.set()
        self.status = "시작 중"
        self.thread = threading.Thread(target=self._loop, daemon=True, name="auto-event")
        self.thread.start()

    def stop(self):
        self.active.clear()

    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def snapshot(self):
        return {"running": self.running(), "status": self.status, "mode": self.mode_override or "auto",
                "dayInterval": self.day_interval, "nightInterval": self.night_interval, "cycles": self.cycles}

    def _ctx(self):
        from shop_comparator.services.jobs import JobContext
        job = {"id": "auto-event", "cancel": _Inverse(self.active), "summary": {}, "progress": {}}
        return JobContext(job, _Lockable())

    def _loop(self):
        from shop_comparator.services.jobs import Cancelled
        with L.bind(job="auto-event", stage="auto-event"):
            L.log("이벤트 트리거 자동화를 시작합니다.")
            last_mode = None
            seeded = False
            while self.active.is_set():
                mode = self.mode()
                if mode != last_mode:
                    self.status = "밤 전체 갱신 모드" if mode == "night_full" else "낮 이벤트 감시 모드"
                    L.log(f"모드 전환: {self.status}")
                    last_mode = mode
                wait = 60
                try:
                    ctx = self._ctx()
                    if not seeded and self.seed:
                        L.log("감시 대상을 새로 수집합니다.")
                        pipeline.collect_musinsa(self.seed, ctx)
                        seeded = True
                    if not store.products():
                        L.warn("감시할 상품이 없습니다. 먼저 무신사 수집을 실행해 주세요.")
                    else:
                        self._baseline()
                        if mode == "night_full":
                            self._night(ctx)
                            wait = self.night_interval
                        else:
                            self._day(ctx)
                            wait = self.day_interval
                    self.cycles += 1
                except Cancelled:
                    break
                except CrawlError as exc:
                    L.error(f"자동 이벤트 주기 실패: {exc}", exc.code)
                    if exc.code == "auth":
                        self.status = "로그인 필요로 중지됨"
                        self.active.clear()
                        break
                except Exception as exc:
                    L.error(f"자동 이벤트 주기 실패: {type(exc).__name__}", "internal")
                if self.active.is_set():
                    L.log(f"다음 주기까지 {wait}초 대기합니다.")
                    end = time.monotonic() + wait
                    while self.active.is_set() and time.monotonic() < end:
                        time.sleep(1)
            if not self.status.startswith("로그인"):
                self.status = "중지됨"
            L.log("자동 이벤트 모드가 종료되었습니다.")

    def _baseline(self):
        for product in store.products():
            goods_no = str(product["goodsNo"])
            if goods_no not in self.last_prices:
                price = product.get("lastObservedFinalPrice") or product.get("finalPrice")
                if price is not None:
                    self.last_prices[goods_no] = int(price)

    def _refresh_musinsa(self, ctx):
        goods = [str(p["goodsNo"]) for p in store.products()]
        pipeline.collect_musinsa({"mode": "selected", "ids": goods, "limit": len(goods), "price_mode": "grouped"}, ctx)
        dropped = []
        for product in store.products(goods_nos=goods):
            goods_no, current = str(product["goodsNo"]), product.get("finalPrice")
            previous = self.last_prices.get(goods_no)
            if current is None:
                continue
            if previous is not None and int(current) < int(previous):
                L.log(f"가격 하락 감지: {previous:,}원 → {int(current):,}원", goods=goods_no)
                dropped.append(goods_no)
            self.last_prices[goods_no] = int(current)
            database.update_last_observed_price(goods_no, int(current))
        return goods, dropped

    def _day(self, ctx):
        _, dropped = self._refresh_musinsa(ctx)
        if dropped:
            try:
                pipeline.collect_kream(dropped, ctx, match_missing=True)
            except CrawlError as exc:
                if exc.code == "auth":
                    L.warn(f"KREAM 비교를 건너뜁니다: {exc}")
                else:
                    raise

    def _night(self, ctx):
        L.log("밤 모드: 현재 상품 전체의 무신사 가격을 갱신합니다.")
        goods, _ = self._refresh_musinsa(ctx)
        L.log("밤 모드: 현재 상품 전체의 KREAM 비교를 실행합니다.")
        try:
            pipeline.collect_kream(goods, ctx, match_missing=True)
        except CrawlError as exc:
            if exc.code in ("auth", "prerequisite"):
                L.warn(f"KREAM 비교를 건너뜁니다: {exc}")
            else:
                raise


class _Inverse:
    """Present an 'active' Event as a job cancel flag."""

    def __init__(self, event):
        self.event = event

    def is_set(self):
        return not self.event.is_set()

    def wait(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if not self.event.is_set():
                return True
            time.sleep(min(0.5, max(0, end - time.monotonic())))
        return not self.event.is_set()


class _Lockable:
    lock = threading.RLock()

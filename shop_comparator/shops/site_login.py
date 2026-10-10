"""Per-shop login with the account the user typed in the web UI.

Musinsa and KREAM are handled independently: separate Chrome profile,
separate status, separate failure handling.  A login only counts as done
after an authenticated request succeeds; a URL change or a cookie alone is
not accepted.

Passwords live in memory only.  They are never written to disk, logs,
responses or the database.  If the shop asks for extra verification
(CAPTCHA, SMS, device check) the visible Chrome window stays open so the
user can finish it there, and the flow continues once the shop accepts it.
"""
import json
import os
import threading
import time
from urllib.parse import urlparse

from selenium.common.exceptions import NoAlertPresentException, UnexpectedAlertPresentException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from shop_comparator.core import app_logging as L
from shop_comparator.shops import crawler
from shop_comparator.storage import database
from shop_comparator.storage import store
from shop_comparator.shops.browser_transport import BrowserSession, new_driver, profile_lock
from shop_comparator.core.crawl_errors import CrawlError
from shop_comparator.core.utils import get_persistent_path

MUSINSA_LOGIN = "https://www.musinsa.com/auth/login"
KREAM_LOGIN = "https://kream.co.kr/login/email"  # /login no longer shows the e-mail form
KREAM_VERIFY_PRODUCT = "74130"
ORDER_FORM = "https://order.musinsa.com/api2/order/v1/orders/order-form"

STATE_LABEL = {
    "logged_in": "로그인됨",
    "expired": "만료됨 · 다시 로그인 필요",
    "failed": "로그인 실패",
    "needs_user": "추가 인증 대기",
    "unknown": "확인 안 됨",
    "checking": "확인 중",
}


class Vault:
    """Optional in-memory credentials for re-login while the server runs."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}

    def put(self, site, account, password):
        L.register_secret(password)
        with self._lock:
            self._items[site] = (account, password)

    def get(self, site):
        with self._lock:
            return self._items.get(site)

    def drop(self, site):
        with self._lock:
            item = self._items.pop(site, None)
        if item:
            L.forget_secret(item[1])

    def has(self, site):
        with self._lock:
            return site in self._items


vault = Vault()


def _type(element, text):
    element.click()
    element.send_keys(Keys.CONTROL, "a")
    element.send_keys(Keys.BACK_SPACE)
    element.send_keys(text)


def _alert_text(driver):
    try:
        alert = driver.switch_to.alert
        text = alert.text
        try:
            alert.accept()
        except WebDriverException:
            pass
        return text
    except NoAlertPresentException:
        return None


def _visible_error(driver, selectors):
    for selector in selectors:
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                text = (element.text or "").strip()
                if text and element.is_displayed():
                    return text[:120]
        except WebDriverException:
            continue
    return None


def _wait(cancel, seconds):
    return cancel.wait(seconds) if cancel is not None else time.sleep(seconds)


# ---------------------------------------------------------------- Musinsa
def _musinsa_has_token(driver):
    try:
        cookies = crawler._get_all_cookies(driver)
        if any(c.get("name") == "app_atk" and c.get("value") for c in cookies):
            return True
        state = crawler._collect_current_auth_state(driver)
        return any(state[k].get("app_atk") for k in ("localStorage", "sessionStorage"))
    except UnexpectedAlertPresentException:
        return False


def verify_musinsa(session):
    """Authenticated check: the order-form API answers 401 without a session.

    Returns (ok, detail).  Any non-auth answer (including "empty cart")
    proves the member session is accepted.
    """
    response = session.get(ORDER_FORM, headers={"accept": "application/json"}, timeout=20)
    if response.status_code in (401, 403):
        return False, f"주문서 API가 인증을 거절했습니다 (HTTP {response.status_code})."
    if response.status_code >= 500:
        raise CrawlError("무신사", "http", f"로그인 확인 중 서버 오류 (HTTP {response.status_code}).")
    return True, f"주문서 API 인증 확인 (HTTP {response.status_code})"


def musinsa_saved_cookies():
    """Login cookies saved at the last login.

    Musinsa's auth cookies (app_atk/app_rtk) are session cookies: Chrome drops
    them when the login window closes, so the profile alone is logged out
    again.  They are re-applied to the browser at the start of each job.
    """
    payload = database.load_auth_session().get("auth_payload") or {}
    return payload.get("cookies") or []


def _save_musinsa(driver):
    payload = crawler._collect_auth_payload(driver)
    payload["user_agent"] = driver.execute_script("return navigator.userAgent")
    header = crawler._merge_cookie_header(crawler._build_cookie_header(payload["cookies"]), payload)
    crawler.set_auth_payload(payload)
    database.save_cookie(header, payload)  # same table the desktop GUI reads
    return payload


def login_musinsa(account, password, cancel=None, wait_seconds=300, remember=False):
    """Log in with the given account. Empty account => manual login in the window."""
    with L.bind(shop="musinsa", stage="login"), profile_lock("musinsa"):
        store.save_session("musinsa", "checking", L.mask_account(account) or None, "로그인 진행 중")
        if password:
            L.register_secret(password)
        driver = new_driver("musinsa", headless=False)
        try:
            driver.get(MUSINSA_LOGIN)
            time.sleep(2)
            auto = bool(account and password)
            if _musinsa_has_token(driver) and "login" not in urlparse(driver.current_url).path:
                L.log("이 브라우저 프로필에 이미 로그인되어 있습니다. 인증 상태를 확인합니다.")
            elif auto:
                L.log(f"무신사 로그인 화면에 계정 정보를 입력합니다 ({L.mask_account(account)}).")
                id_input = _find(driver, ["input[title='통합계정 또는 이메일']", "input[type='text'][autocomplete='off']", "input[type='text']"], 60)
                pw_input = _find(driver, ["input[title='비밀번호 입력']", "input[type='password']"], 20)
                _type(id_input, account)
                _type(pw_input, password)
                time.sleep(0.5)
                pw_input.send_keys(Keys.RETURN)
            else:
                L.log("열린 Chrome 창에서 무신사에 직접 로그인해 주세요.")
            result = _await_musinsa(driver, cancel, wait_seconds, auto)
            if result["state"] != "ok":
                store.save_session("musinsa", "unknown" if result.get("kind") == "cancelled" else result["state"], None, result["message"])
                L.error(result["message"], result.get("kind", "auth"))
                raise CrawlError("무신사", result.get("kind", "auth"), result["message"])
            _save_musinsa(driver)
            session = BrowserSession("musinsa", driver=driver)
            ok, detail = verify_musinsa(session)
            if not ok:
                store.save_session("musinsa", "failed", None, detail)
                raise CrawlError("무신사", "auth", "로그인은 되었지만 회원 전용 조회가 거절되었습니다. " + detail)
            store.save_session("musinsa", "logged_in", L.mask_account(account) or None, detail, verified=True)
            if remember and auto:
                vault.put("musinsa", account, password)
            L.log("무신사 로그인 완료. " + detail)
            return {"state": "logged_in", "detail": detail}
        finally:
            if password and not (remember and vault.has("musinsa")):
                L.forget_secret(password)
            try:
                driver.quit()
            except WebDriverException:
                pass


def _find(driver, selectors, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for selector in selectors:
            try:
                for element in driver.find_elements(By.CSS_SELECTOR, selector):
                    if element.is_displayed():
                        return element
            except WebDriverException:
                pass
        time.sleep(0.5)
    raise CrawlError("로그인", "schema", "로그인 입력란을 찾지 못했습니다. 로그인 화면 구조가 바뀌었을 수 있습니다.")


def _await_musinsa(driver, cancel, wait_seconds, auto):
    start = time.monotonic()
    notified = False
    while time.monotonic() - start < wait_seconds:
        if cancel is not None and cancel.is_set():
            return {"state": "failed", "kind": "cancelled", "message": "로그인을 취소했습니다."}
        text = _alert_text(driver)
        if text:
            return {"state": "failed", "kind": "auth", "message": f"무신사가 로그인을 거절했습니다: {text[:120]}"}
        if _musinsa_has_token(driver):
            return {"state": "ok"}
        error = _visible_error(driver, [".login-v2-input__error", "[class*='error-message']", "[class*='error_message']", "[role='alert']"])
        if auto and error and time.monotonic() - start > 2:
            return {"state": "failed", "kind": "auth", "message": f"무신사 로그인 실패: {error}"}
        if not notified and time.monotonic() - start > (15 if auto else 0):
            notified = True
            store.save_session("musinsa", "needs_user", None, "열린 Chrome 창에서 추가 인증을 완료해 주세요." if auto else "열린 Chrome 창에서 로그인해 주세요.")
            L.warn("추가 인증(CAPTCHA·본인확인)이 필요할 수 있습니다. 열린 Chrome 창에서 완료하면 자동으로 이어집니다.")
        _wait(cancel, 1)
    return {"state": "failed", "kind": "timeout", "message": f"{wait_seconds}초 안에 로그인이 완료되지 않았습니다. 다시 시도해 주세요."}


# ------------------------------------------------------------------ KREAM
def _kream_token(driver):
    from shop_comparator.shops import kream_login
    access, device, trace, baggage = kream_login._extract_token_and_device_id_from_localstorage(driver)
    return access, device, trace, baggage


def kream_api_headers(session):
    """Headers the logged-in KREAM page itself sends, plus stored fallbacks."""
    from shop_comparator.shops import kream_crawler
    headers = {
        "accept": "*/*",
        "x-kream-api-version": kream_crawler.KREAM_API_VERSION,
        "x-kream-web-build-version": kream_crawler.KREAM_WEB_BUILD_VER,
        "x-kream-web-request-secret": kream_crawler.KREAM_WEB_REQUEST_SEC,
        "x-kream-device-id": kream_crawler.KREAM_DEVICE_ID,
        "x-kream-client-datetime": kream_crawler.kst_ts(),
    }
    headers.update(kream_crawler.load_observed_headers())
    headers.pop("user-agent", None)
    return headers


def verify_kream(session, token=None):
    """Authenticated check: the sales-history API answers 401 without a member token.

    (Per-size quotes and search are public, so they cannot prove a login.)
    """
    import uuid
    headers = kream_api_headers(session)
    if token and "authorization" not in session.observed:
        headers["authorization"] = f"Bearer {token}"
    if "authorization" not in headers and "authorization" not in session.observed:
        return False, "브라우저 프로필에 KREAM 로그인 정보가 없습니다."
    url = (f"https://api.kream.co.kr/api/p/products/{KREAM_VERIFY_PRODUCT}/sales?cursor=1&per_page=50"
           "&sort=date_created%5Bdesc%5D&request_key=" + str(uuid.uuid4()))
    response = session.get(url, headers=headers, timeout=20)
    if response.status_code in (401, 403):
        return False, f"체결 내역 API가 인증을 거절했습니다 (HTTP {response.status_code})."
    if response.status_code >= 400:
        raise CrawlError("KREAM", "http", f"로그인 확인 중 오류 (HTTP {response.status_code}).")
    try:
        data = response.json()
    except ValueError:
        raise CrawlError("KREAM", "format", "로그인 확인 응답이 JSON이 아닙니다.")
    if not isinstance(data, dict) or "items" not in data:
        return False, "체결 내역 API 응답 형식이 예상과 다릅니다."
    return True, "체결 내역 API 인증 확인 (HTTP 200)"


def kream_member_token(session):
    """Live member token of this profile, or None.

    Product pages are server-rendered and send no API request, so when the
    token is not in localStorage the member page is opened once and the
    Authorization header of the site's own request is read.
    """
    driver = session.driver
    session._observe_headers()
    bearer = session.observed.get("authorization", "")
    token = bearer[7:] if bearer.lower().startswith("bearer ") else _kream_token(driver)[0]
    if token:
        return token
    try:
        driver.get_log("performance")
        driver.get("https://kream.co.kr/my")
        time.sleep(3)
        if "/login" in urlparse(driver.current_url).path:
            return None
        return _kream_network_token(driver) or _kream_token(driver)[0]
    except WebDriverException:
        return None


def save_kream_session(session, known_token=None):
    """Return the live member token of this browser profile (None if logged out).

    When logged in, cookies/token are also written to the files the desktop
    GUI reads.  When logged out nothing is written: existing files stay as is.
    """
    from shop_comparator.shops import kream_crawler
    driver = session.driver
    session._observe_headers()
    access, device, trace, baggage = _kream_token(driver)
    observed = dict(session.observed)
    bearer = observed.get("authorization", "")
    if bearer.lower().startswith("bearer "):
        access = bearer[7:]
    access = access or known_token
    kream_crawler.set_session_token(access)
    if not access:
        return None
    cookies = session.cookies()
    with open(get_persistent_path("cookies.json"), "w", encoding="utf-8") as f:
        json.dump(cookies, f)
    refresh = next((c.get("value") for c in cookies if str(c.get("name", "")).startswith("_refresh_token.local")), None)
    with open(get_persistent_path("cookies_token.json"), "w", encoding="utf-8") as f:
        json.dump({
            "saved_at": int(time.time()),
            "user_agent": driver.execute_script("return navigator.userAgent"),
            "access_token": access, "refresh_token": refresh,
            "device_id": observed.get("x-kream-device-id", device),
            "sentry_trace": trace, "baggage": baggage,
            "request_headers": {k: v for k, v in observed.items() if k != "authorization"},
        }, f)
    return access


def _kream_network_token(driver):
    """Bearer token from the page's own API requests (Chrome performance log)."""
    token = None
    try:
        for entry in driver.get_log("performance"):
            message = json.loads(entry["message"])["message"]
            if message.get("method") != "Network.requestWillBeSent":
                continue
            request = message.get("params", {}).get("request", {})
            if urlparse(request.get("url", "")).hostname != "api.kream.co.kr":
                continue
            for key, value in request.get("headers", {}).items():
                if key.lower() == "authorization" and str(value).lower().startswith("bearer "):
                    token = value[7:]
    except WebDriverException:
        pass
    return token


def _kream_logged_in(driver, state):
    """True once the member token is visible.

    The token is looked for in localStorage and in the Authorization header
    of the page's own API calls.  If the page has left the login screen but
    no token was seen yet, the member page is opened once to make the site
    issue an authenticated request.
    """
    url = urlparse(driver.current_url)
    if not (url.hostname or "").endswith("kream.co.kr") or "/login" in url.path:
        return False
    token = _kream_network_token(driver) or _kream_token(driver)[0]
    if not token and not state.get("probed"):
        state["probed"] = True
        try:
            driver.get("https://kream.co.kr/my")
            time.sleep(3)
        except WebDriverException:
            return False
        if "/login" in urlparse(driver.current_url).path:
            state["probed"] = False  # not logged in yet: keep waiting on the login screen
            return False
        token = _kream_network_token(driver) or _kream_token(driver)[0]
    if token:
        state["token"] = token
    return bool(token)


def login_kream(account, password, cancel=None, wait_seconds=300, remember=False):
    with L.bind(shop="kream", stage="login"), profile_lock("kream"):
        store.save_session("kream", "checking", L.mask_account(account) or None, "로그인 진행 중")
        if password:
            L.register_secret(password)
        driver = new_driver("kream", headless=False)
        try:
            driver.get(KREAM_LOGIN)
            time.sleep(3)
            auto = bool(account and password)
            if "/login" not in urlparse(driver.current_url).path:
                L.log("이 브라우저 프로필에 이미 로그인되어 있습니다. 인증 상태를 확인합니다.")
            elif auto:
                L.log(f"KREAM 이메일 로그인 화면에 계정 정보를 입력합니다 ({L.mask_account(account)}).")
                email = _find(driver, ["main input[type='email']", "input[type='email']"], 30)
                secret = _find(driver, ["main input[type='password']", "input[type='password']"], 10)
                _type(email, account)
                _type(secret, password)
                time.sleep(0.8)
                secret.send_keys(Keys.RETURN)
            else:
                L.log("열린 Chrome 창에서 KREAM에 직접 로그인해 주세요.")
            start = time.monotonic()
            notified = False
            seen = {}
            while True:
                if cancel is not None and cancel.is_set():
                    store.save_session("kream", "unknown", None, "로그인을 취소했습니다.")
                    raise CrawlError("KREAM", "cancelled", "로그인을 취소했습니다.")
                if time.monotonic() - start > wait_seconds:
                    store.save_session("kream", "failed", None, "로그인 대기 시간 초과")
                    raise CrawlError("KREAM", "timeout", f"{wait_seconds}초 안에 로그인이 완료되지 않았습니다. 다시 시도해 주세요.")
                if _kream_logged_in(driver, seen):
                    break
                error = _visible_error(driver, [".input_error", ".toast", "[class*='toast']", "[role='alert']"])
                if auto and error and time.monotonic() - start > 3:
                    store.save_session("kream", "failed", None, error)
                    raise CrawlError("KREAM", "auth", f"KREAM 로그인 실패: {error}")
                if not notified and time.monotonic() - start > (15 if auto else 0):
                    notified = True
                    store.save_session("kream", "needs_user", None, "열린 Chrome 창에서 추가 인증을 완료해 주세요." if auto else "열린 Chrome 창에서 로그인해 주세요.")
                    if auto:
                        L.warn("추가 인증이 필요할 수 있습니다. 열린 Chrome 창에서 완료하면 자동으로 이어집니다.")
                _wait(cancel, 1)
            session = BrowserSession("kream", driver=driver, start_url=f"https://kream.co.kr/products/{KREAM_VERIFY_PRODUCT}")
            time.sleep(3)
            token = save_kream_session(session, seen.get("token"))
            ok, detail = verify_kream(session, token)
            if not ok:
                store.save_session("kream", "failed", None, detail)
                raise CrawlError("KREAM", "auth", "로그인은 되었지만 회원 전용 조회가 거절되었습니다. " + detail)
            store.save_session("kream", "logged_in", L.mask_account(account) or None, detail, verified=True)
            if remember and auto:
                vault.put("kream", account, password)
            L.log("KREAM 로그인 완료. " + detail)
            return {"state": "logged_in", "detail": detail}
        finally:
            if password and not (remember and vault.has("kream")):
                L.forget_secret(password)
            try:
                driver.quit()
            except WebDriverException:
                pass


# ---------------------------------------------------------------- status
def check_session(site):
    """Re-validate a stored session with a real authenticated request."""
    from shop_comparator.shops.browser_transport import browser_transport
    with L.bind(shop=site, stage="session-check"):
        try:
            if site == "musinsa":
                with browser_transport("musinsa", cookies=musinsa_saved_cookies()) as session:
                    ok, detail = verify_musinsa(session)
            else:
                with browser_transport("kream", start_url=f"https://kream.co.kr/products/{KREAM_VERIFY_PRODUCT}") as session:
                    time.sleep(3)
                    ok, detail = verify_kream(session, save_kream_session(session, kream_member_token(session)))
        except CrawlError as exc:
            if exc.code == "busy":
                raise
            store.save_session(site, "unknown", None, str(exc))
            L.error(f"세션 확인 실패: {exc}", exc.code)
            raise
        store.save_session(site, "logged_in" if ok else "expired", None, detail, verified=ok)
        L.log(f"세션 확인 결과: {'유효' if ok else '만료'} · {detail}", level="info" if ok else "warning")
        return {"state": "logged_in" if ok else "expired", "detail": detail}


def mark_expired(site, detail):
    store.save_session(site, "expired", None, detail)


def relogin_if_possible(site, cancel=None):
    """Use credentials kept in memory (opt-in) to log in again. Returns bool."""
    item = vault.get(site)
    if not item:
        return False
    L.warn("세션이 만료되어 메모리에 보관한 계정으로 다시 로그인합니다.", shop=site)
    try:
        (login_musinsa if site == "musinsa" else login_kream)(item[0], item[1], cancel, remember=True)
        return True
    except CrawlError as exc:
        L.error(f"자동 재로그인 실패: {exc}", exc.code, shop=site)
        return False


def status():
    saved = store.sessions()
    out = {}
    for site in ("musinsa", "kream", "poizon"):
        row = saved.get(site) or {}
        state = row.get("state") or "unknown"
        if state in ("checking", "needs_user"):
            from shop_comparator.shops.browser_transport import is_busy
            if site in ("musinsa", "kream") and not is_busy(site):
                state = "unknown"  # a previous server process stopped mid-login
        label = STATE_LABEL.get(state, state)
        if state == "expired" and not row.get("verifiedAt"):
            label = "로그인 필요"  # never verified on this PC: not really "expired"
        out[site] = {
            "state": state, "label": label, "account": row.get("account") or "",
            "verifiedAt": row.get("verifiedAt"), "detail": row.get("detail") or "",
            "remembered": vault.has(site),
            "profile": os.path.isdir(get_persistent_path(os.path.join("browser_profiles", site))) if site != "poizon" else None,
        }
    return out

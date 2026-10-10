"""HTTP-compatible adapter that sends requests from an ordinary Chrome page.

Why a browser: plain HTTP clients get 403 (Musinsa) or an empty 500 (KREAM)
on this network, while the same requests succeed from a normal Chrome page.

Each shop has its own persistent Chrome profile (browser_profiles/<shop>,
git-ignored) so a login survives server restarts.  Only one Chrome may use a
profile at a time; `profile_lock` enforces that.

No stealth flags, challenge solving or proxy rotation.  navigator.webdriver
stays true and access denials stay errors.  KREAM answers HTTP 500 to
headless Chrome, so the KREAM window is a normal window opened off-screen;
it does not take keyboard focus from the user.
"""
import json
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlencode, urlparse

import requests
from selenium import webdriver

from shop_comparator.core.crawl_errors import CrawlError, check_status
from shop_comparator.core.utils import get_persistent_path

SHOPS = {
    "musinsa": {"host": "www.musinsa.com", "root": "musinsa.com", "label": "무신사"},
    "kream": {"host": "kream.co.kr", "root": "kream.co.kr", "label": "KREAM"},
}
_active = ContextVar("shop_browser_transports", default={})
_locks = {name: threading.Lock() for name in SHOPS}
_KREAM_HEADERS = {"authorization", "x-kream-api-version", "x-kream-web-build-version",
                  "x-kream-web-request-secret", "x-kream-device-id"}


def current(service):
    return _active.get().get(service)


def profile_dir(service):
    return get_persistent_path(os.path.join("browser_profiles", service))


def headless_default(service):
    """Musinsa works headless; KREAM returns HTTP 500 to headless Chrome."""
    override = os.getenv(f"SHOP_{service.upper()}_HEADLESS")
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes", "on"}
    return service == "musinsa"


@contextmanager
def profile_lock(service, wait=0):
    """Serialise use of one shop's Chrome profile."""
    lock = _locks[service]
    if not lock.acquire(timeout=wait) if wait else not lock.acquire(blocking=False):
        raise CrawlError(SHOPS[service]["label"], "busy", "다른 작업이 이 쇼핑몰 브라우저를 사용 중입니다. 끝난 뒤 다시 시도해 주세요.")
    try:
        yield
    finally:
        lock.release()


def is_busy(service):
    return _locks[service].locked()


def _foreground_window():
    """Handle of the window the user is working in (Windows only)."""
    try:
        import ctypes
        return ctypes.windll.user32.GetForegroundWindow()
    except Exception:
        return None


def _restore_foreground(handle):
    """Give keyboard focus back to the user's window after Chrome has started."""
    if not handle:
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        if user32.GetForegroundWindow() != handle:
            user32.SetForegroundWindow(handle)
    except Exception:
        pass


def new_driver(service, headless=None, use_profile=True, background=False):
    """Start Chrome.  background=True is for data collection in a normal
    (non-headless) window: it opens off-screen and focus returns to whatever
    the user was doing.  Login windows are opened normally (background=False)."""
    options = webdriver.ChromeOptions()
    is_headless = headless_default(service) if headless is None else headless
    if is_headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1280,900")
    if background and not is_headless:
        options.add_argument("--window-position=-32000,-32000")
    previous = _foreground_window() if background and not is_headless else None
    if use_profile:
        path = profile_dir(service)
        os.makedirs(path, exist_ok=True)
        options.add_argument(f"--user-data-dir={path}")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(40)
    _restore_foreground(previous)
    return driver


class BrowserSession:
    """requests-like get/post executed with fetch() inside the shop's page."""

    def __init__(self, service, cookies=None, auth_payload=None, headless=None, use_profile=True, driver=None, start_url=None,
                 background=False):
        self.service = service
        self.headers = {}
        self.observed = {}
        self.pages = {}
        self.driver = None
        self.host = SHOPS[service]["host"]
        self.root = SHOPS[service]["root"]
        try:
            previous = _foreground_window() if background else None
            self.driver = driver or new_driver(service, headless, use_profile, background=background)
            valid_cookies = []
            for cookie in cookies or []:
                domain = str(cookie.get("domain", "")).lstrip(".")
                if domain != self.root and not domain.endswith("." + self.root):
                    continue
                valid_cookies.append({k: v for k, v in cookie.items()
                                      if k in {"name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "expires"}})
                if "expiry" in cookie:
                    valid_cookies[-1]["expires"] = cookie["expiry"]
            if valid_cookies:
                self.driver.execute_cdp_cmd("Network.setCookies", {"cookies": valid_cookies})
            self.driver.get(start_url or f"https://{self.host}/")
            self.pages[self.host] = self.driver.current_window_handle
            self._active_page = self.pages[self.host]
            _restore_foreground(previous)
            for origin, values in (auth_payload or {}).get("origins", {}).items():
                if urlparse(origin).hostname != self.host:
                    continue
                self.driver.execute_script("""
                    for (const area of ['localStorage','sessionStorage'])
                      for (const [key,value] of Object.entries(arguments[0][area] || {}))
                        window[area].setItem(key,value);
                """, values)
            self._observe_headers()
            if service == "kream":
                self._read_kream_config()
        except Exception:
            self.close()
            raise

    def _read_kream_config(self):
        """API/build version from the page's own embedded config.

        The product page is server-rendered and makes no API call on load,
        so there is nothing to observe on the network; the values the site
        will use are in its inline `__NUXT__.config`.  Outdated versions are
        answered with 403.
        """
        try:
            found = self.driver.execute_script(r"""
                for (const s of document.scripts) {
                  const t = s.textContent;
                  if (!t.includes('apiVersion')) continue;
                  const pick = name => (t.match(new RegExp(name + '\s*:\s*"([^"]+)"')) || [])[1];
                  return {api: pick('apiVersion'), build: pick('buildVersion'), secret: pick('webRequestSecret')};
                }
                return null;""") or {}
        except Exception:
            found = {}
        for key, name in (("api", "x-kream-api-version"), ("build", "x-kream-web-build-version"), ("secret", "x-kream-web-request-secret")):
            if found.get(key):
                self.observed.setdefault(name, found[key])
        try:  # the site sends "web;<webDid cookie>" as its device id
            device = (self.driver.get_cookie("webDid") or {}).get("value")
            if device:
                self.observed.setdefault("x-kream-device-id", f"web;{device}")
        except Exception:
            pass
        return found

    def _observe_headers(self):
        for entry in self.driver.get_log("performance"):
            message = json.loads(entry["message"])["message"]
            params = message.get("params", {})
            response = params.get("response", {})
            if (message.get("method") == "Network.responseReceived" and params.get("type") == "Document"
                    and urlparse(response.get("url", "")).hostname == self.host):
                check_status(SHOPS[self.service]["label"], int(response.get("status", 200)))
            req = message.get("params", {}).get("request", {})
            if message.get("method") != "Network.requestWillBeSent" or urlparse(req.get("url", "")).hostname != "api.kream.co.kr":
                continue
            for key, value in req.get("headers", {}).items():
                if key.lower() in _KREAM_HEADERS:
                    self.observed[key.lower()] = value

    def cookies(self):
        try:
            found = self.driver.execute_cdp_cmd("Network.getAllCookies", {}).get("cookies") or []
        except Exception:
            found = self.driver.get_cookies()
        return [c for c in found if str(c.get("domain", "")).lstrip(".") == self.root
                or str(c.get("domain", "")).endswith("." + self.root)]

    def request(self, method, url, **kwargs):
        host = urlparse(url).hostname or ""
        root = SHOPS[self.service]["root"]
        if host != root and not host.endswith("." + root):
            raise ValueError("쇼핑몰 외부 요청은 허용하지 않습니다.")
        # Order/cart APIs are consumed on their own origin. Request them there
        # from the outset: retrying a CORS-failed POST could add an item twice.
        origin = host if host in {"order.musinsa.com", "cart.musinsa.com"} else self.host
        if origin not in self.pages:
            self.driver.switch_to.new_window("tab")
            self.driver.get(url.split("?", 1)[0])  # GET only; no cart/order mutation.
            if urlparse(self.driver.current_url).hostname != origin:
                raise CrawlError(SHOPS[self.service]["label"], "auth", "주문/장바구니 화면에서 로그인 확인이 필요합니다. 다시 로그인해 주세요.")
            self.pages[origin] = self.driver.current_window_handle
            self._active_page = self.pages[origin]
        elif getattr(self, "_active_page", None) != self.pages[origin]:
            # switch_to.window() activates a visible Chrome window and steals the
            # user's keyboard focus, so it is only called when the tab really changes.
            self.driver.switch_to.window(self.pages[origin])
            self._active_page = self.pages[origin]
        params = kwargs.get("params")
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params, doseq=True)
        headers = {k.lower(): v for k, v in {**self.headers, **(kwargs.get("headers") or {})}.items()}
        self._observe_headers()
        headers.update(self.observed)
        # Chrome owns these headers and its cookie jar.
        headers = {k: str(v) for k, v in headers.items()
                   if k not in {"cookie", "host", "origin", "referer", "user-agent", "connection", "accept-encoding", "content-length"}
                   and not k.startswith("sec-")}
        body = None
        if "json" in kwargs:
            body = json.dumps(kwargs["json"])
            headers["content-type"] = "application/json"
        timeout = kwargs.get("timeout", 20)
        # api.kream.co.kr's CORS preflight does not allow credentialed requests
        # (PreflightInvalidAllowCredentials); the site authenticates with the
        # Authorization header instead of cookies.
        credentials = "omit" if host == "api.kream.co.kr" else "include"
        self.driver.set_script_timeout(timeout + 5)
        result = self.driver.execute_async_script("""
            const [url, method, headers, body, timeout, credentials, done] = arguments;
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeout);
            fetch(url, {method, headers, body, credentials, signal:controller.signal})
              .then(async response => done({status:response.status, body:await response.text(),
                headers:Object.fromEntries(response.headers.entries())}))
              .catch(() => done({error:true})).finally(() => clearTimeout(timer));
        """, url, method.upper(), headers, body, int(timeout * 1000), credentials)
        if result.get("error"):
            raise CrawlError(SHOPS[self.service]["label"], "network", "브라우저 요청에 실패했습니다. 사이트 접속/인증 상태를 확인해 주세요.")
        response = requests.Response()
        response.status_code = result["status"]
        response._content = result["body"].encode("utf-8")
        response.encoding = "utf-8"
        response.headers.update(result["headers"])
        response.url = url
        return response

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            finally:
                self.driver = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


@contextmanager
def browser_transport(service, cookies=None, auth_payload=None, headless=None, use_profile=True, wait=0, start_url=None):
    """Open the shop's Chrome, make it the transport for this thread, close it after."""
    with profile_lock(service, wait) if use_profile else _nolock():
        with BrowserSession(service, cookies, auth_payload, headless, use_profile, start_url=start_url, background=True) as session:
            token = _active.set({**_active.get(), service: session})
            try:
                yield session
            finally:
                _active.reset(token)


@contextmanager
def _nolock():
    yield

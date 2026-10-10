import json
import logging
import os
import re
import sys
import time

from playwright.sync_api import TimeoutError, sync_playwright

SEARCH_BUTTON_SELECTORS = [
    "#root > div > div.layoutBody___SHdzY > div.layoutRight___jH6ww > div > div > div.mfContainer___ngfO1 > div > div > div.tablePart___qJf0a > div.global-table-v2-wrapper._newChildTableStyle_10yc5_40 > form > button",
    "xpath=/html/body/div[1]/div/div[2]/div[2]/div/div/div[2]/div/div/div[2]/div[2]/form/button",
    "form > button[title=\"Search & List\"]",
    "button[title=\"Search & List\"].ant-btn.ant-btn-primary.ant-btn-sm",
]


def _iter_browser_executable_candidates(headless=False):
    env_browser = os.environ.get("PLAYWRIGHT_BROWSER_EXECUTABLE")
    if env_browser:
        yield env_browser

    browser_roots = []
    env_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env_root:
        browser_roots.append(env_root)

    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        browser_roots.extend(
            [
                os.path.join(exe_dir, "_internal", "browsers"),
                os.path.join(exe_dir, "browsers"),
            ]
        )

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        browser_roots.append(os.path.join(local_app_data, "ms-playwright"))

    for root in browser_roots:
        if not root or not os.path.isdir(root):
            continue

        try:
            names = os.listdir(root)
            chromium_names = sorted([name for name in names if name.startswith("chromium-")], reverse=True)
            headless_shell_names = sorted(
                [name for name in names if name.startswith("chromium_headless_shell-")],
                reverse=True,
            )

            if headless:
                for name in headless_shell_names:
                    yield os.path.join(root, name, "chrome-headless-shell-win64", "chrome-headless-shell.exe")

            for name in chromium_names:
                yield os.path.join(root, name, "chrome-win", "chrome.exe")
                yield os.path.join(root, name, "chrome-win64", "chrome.exe")
        except Exception:
            continue

    program_files = [
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("LOCALAPPDATA"),
    ]
    chrome_rel_paths = [
        os.path.join("Google", "Chrome", "Application", "chrome.exe"),
        os.path.join("Chromium", "Application", "chrome.exe"),
        os.path.join("Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    for base in program_files:
        if not base:
            continue
        for rel_path in chrome_rel_paths:
            yield os.path.join(base, rel_path)


def _resolve_browser_executable(log_callback, headless=False):
    checked = set()
    for candidate in _iter_browser_executable_candidates(headless=headless):
        if not candidate or candidate in checked:
            continue
        checked.add(candidate)
        if os.path.exists(candidate):
            log_callback(f"[POIZON SELLER] Browser executable: {candidate}")
            return candidate
    return None


class PoizonSellerCrawler:
    def __init__(self, log_callback=print):
        self.log = log_callback
        self.browser = None
        self.context = None
        self.page = None
        self.playwright = None

    def start_browser(self, headless=False, cookies_path=None):
        if self.playwright is None:
            self.playwright = sync_playwright().start()

        if self.browser is None:
            launch_args = {
                "headless": headless,
                "args": [
                    "--disable-gpu",
                    "--disable-gpu-compositing",
                    "--disable-dev-shm-usage",
                    "--disable-features=VizDisplayCompositor,UseSkiaRenderer,CanvasOopRasterization",
                    "--disable-accelerated-2d-canvas",
                    "--disable-accelerated-video-decode",
                    "--disable-d3d11",
                    "--no-sandbox",
                ],
            }
            target_browser_path = _resolve_browser_executable(self.log, headless=headless)

            if target_browser_path:
                launch_args["executable_path"] = target_browser_path
            else:
                self.log("[WARN] No browser executable found in bundled/local paths. Letting Playwright decide.")

            try:
                self.browser = self.playwright.chromium.launch(**launch_args)
            except Exception as e:
                if "WinError 2" in str(e) or "executable doesn't exist" in str(e):
                    self.log(f"[WARN] Explicit browser launch failed, retrying default Playwright lookup: {e}")
                    launch_args.pop("executable_path", None)
                    self.browser = self.playwright.chromium.launch(**launch_args)
                else:
                    raise

            self.context = self.browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )

            if cookies_path and os.path.exists(cookies_path):
                try:
                    with open(cookies_path, "r", encoding="utf-8") as f:
                        cookies = json.load(f)
                        self.context.add_cookies(cookies)
                    self.log(f"[POIZON SELLER] Cookies loaded: {cookies_path}")
                except Exception as e:
                    self.log(f"[POIZON SELLER] Cookie load failed: {e}")

            self.page = self.context.new_page()

    def save_cookies(self, path):
        if self.context:
            try:
                cookies = self.context.cookies()
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(cookies, f, indent=2)
                self.log(f"[POIZON SELLER] Cookies saved: {path}")
                return True
            except Exception as e:
                self.log(f"[POIZON SELLER] Cookie save failed: {e}")
                return False
        return False

    def _click_search_button(self):
        last_error = None
        for selector in SEARCH_BUTTON_SELECTORS:
            try:
                button = self.page.locator(selector).first
                if button.count() == 0:
                    continue
                button.wait_for(state="visible", timeout=5000)
                button.scroll_into_view_if_needed()
                self.page.wait_for_timeout(200)
                try:
                    button.click(force=True, timeout=5000)
                except Exception:
                    handle = button.element_handle(timeout=5000)
                    if handle:
                        self.page.evaluate("(el) => el.click()", handle)
                    else:
                        raise
                self.log(f"[POIZON SELLER] Clicked search button via selector: {selector}")
                return True
            except Exception as e:
                last_error = e
                continue
        if last_error:
            raise last_error
        raise RuntimeError("Search button not found")

    def login(self, email, password, cookies_path=None):
        if not self.page:
            self.start_browser(headless=True, cookies_path=cookies_path)

        self.log("[POIZON SELLER] Opening login page...")
        self.page.goto("https://seller.poizon.com/")

        try:
            if "/main" in self.page.url:
                self.log("[POIZON SELLER] Already logged in.")
                return True

            self.log("[POIZON SELLER] Filling credentials...")

            self.page.wait_for_selector("#mobile_number", state="visible", timeout=10000)
            self.page.click("#mobile_number")
            self.page.fill("#mobile_number", email)

            self.page.wait_for_selector("#password", state="visible", timeout=5000)
            self.page.fill("#password", password)

            self.log("[POIZON SELLER] Clicking login button...")

            try:
                login_button = self.page.get_by_role(
                    "button", name=re.compile(r"Log In|로그인", re.IGNORECASE)
                ).first
                if not login_button.is_visible():
                    login_button = self.page.locator("button[type='submit']").first
            except Exception:
                login_button = self.page.locator("button[type='submit']").first

            login_button.wait_for(state="visible", timeout=30000)
            login_button.click()

            self.page.wait_for_url("**/main/**", timeout=60000)
            self.log("[POIZON SELLER] Login success.")

            if cookies_path:
                self.save_cookies(cookies_path)

            return True

        except Exception as e:
            self.log(f"[POIZON SELLER] Login failed: {e}")
            try:
                self.page.screenshot(path="poizon_login_fail.png")
            except Exception:
                pass
            return False

    def _perform_scraping_logic(self):
        """
        Executes the common scraping logic:
        1. Set up API listener.
        2. Click 'Select All'.
        3. Wait for data or fallback to DOM.
        4. Return scraped data and current URL.
        """
        scraped_data = {}
        api_response_captured = False

        def handle_response(response):
            nonlocal scraped_data, api_response_captured
            if (
                response.status == 200
                and response.headers.get("content-type", "").startswith("application/json")
                and (
                    "price" in response.url
                    or "bid" in response.url
                    or "sku" in response.url
                    or "list" in response.url
                    or "option" in response.url
                )
                and not (
                    "metrics" in response.url or "traces" in response.url or "logs" in response.url
                )
            ):
                try:
                    json_response = response.json()
                    data_content = json_response.get("data")
                    if not data_content and isinstance(json_response, dict):
                        data_content = json_response

                    if isinstance(data_content, dict) and "resultList" in data_content:
                        data_content = data_content["resultList"]

                    if isinstance(data_content, list):
                        for item in data_content:
                            size_key = None
                            price_key = None

                            for key in ["sizeName", "sizeValue", "option", "size", "skuSize"]:
                                if key in item and isinstance(item[key], str):
                                    size_key = key
                                    break

                            for key in ["price", "bidPrice", "value", "amount", "finalPrice"]:
                                if key in item and (
                                    isinstance(item[key], int) or isinstance(item[key], float)
                                ):
                                    price_key = key
                                    break

                            if size_key and price_key:
                                size_val = item[size_key].strip()
                                price_val = int(item[price_key])
                                if size_val and price_val > 0:
                                    scraped_data[size_val] = price_val
                                    api_response_captured = True
                                    self.log(f"[POIZON SELLER][API] Captured {size_val} - {price_val}")

                        if scraped_data:
                            self.log("[POIZON SELLER][API] Price API response captured and parsed.")
                            api_response_captured = True

                except Exception as e:
                    self.log(f"[POIZON SELLER][API] Response parse failed: {e} (URL: {response.url})")

        self.page.on("response", handle_response)
        self.log("[POIZON SELLER] API response handler registered.")

        try:
            select_all_button = self.page.locator(
                ".global-text-label-wrap.global-text-label-wrap-small > "
                "div.global-text-label-wrap-main:has-text('Select All')"
            ).first
            select_all_button.wait_for(state="visible", timeout=15000)
            select_all_button.click()
            self.log("[POIZON SELLER] Clicked 'Select All'.")

            start_time = time.time()
            while not api_response_captured and (time.time() - start_time < 5):
                self.page.wait_for_timeout(500)

            self.page.remove_listener("response", handle_response)
            self.log("[POIZON SELLER] API response handler removed.")

            if not scraped_data:
                self.log("[POIZON SELLER] API capture failed, falling back to DOM scraping.")
                table_body_selector = ".ant-table-container > div.ant-table-body > table > tbody"
                table_body = self.page.locator(table_body_selector)
                data_rows_locator = table_body.locator("tr:not(.ant-table-measure-row)")

                try:
                    data_rows_locator.first.wait_for(state="visible", timeout=5000)
                    rows = data_rows_locator.all()

                    self.log(f"[POIZON SELLER][FALLBACK] Found {len(rows)} rows.")

                    for row_index, row_locator in enumerate(rows):
                        size_text = ""
                        price_text = ""
                        try:
                            size_locator = row_locator.locator(
                                "td.ant-table-cell.rowSpanTd___idFZy "
                                "> div > div.ant-space.ant-space-vertical > div:nth-child(1) > div"
                            ).first
                            size_text = (size_locator.text_content(timeout=300) or "").strip()

                            price_locator = row_locator.locator(
                                "td:nth-child(8) > div > div:nth-child(2) > div > "
                                "div:nth-child(2) > span > div > div:nth-child(1) > div > div"
                            ).first
                            price_text = (price_locator.text_content(timeout=300) or "").strip()

                            if not price_text:
                                td_count = row_locator.locator("td").count()
                                for td_idx in range(td_count):
                                    candidate = (
                                        row_locator.locator("td").nth(td_idx).text_content(timeout=200) or ""
                                    ).strip()
                                    if "¥" in candidate or "￥" in candidate:
                                        price_text = candidate
                                        break

                            if size_text and price_text:
                                price_clean = re.sub(r"[^\d]", "", price_text)
                                if price_clean:
                                    scraped_data[size_text] = int(price_clean)
                            elif row_index == 0:
                                self.log(
                                    f"[POIZON SELLER][FALLBACK][DEBUG] "
                                    f"first_row size={size_text!r} price={price_text!r}"
                                )
                        except Exception:
                            pass
                except TimeoutError:
                    self.log("[POIZON SELLER] DOM scraping fallback failed.")

            self.log(f"[POIZON SELLER] Collected {len(scraped_data)} size prices.")
            return scraped_data, self.page.url

        except Exception as e:
            self.log(f"[POIZON SELLER] Scraping logic failed: {e}")
            return scraped_data, self.page.url

    def search_and_scrape(self, serial_number):
        if not self.page:
            self.log("[POIZON SELLER] Browser has not been started.")
            return {}, ""

        target_url = "https://seller.poizon.com/main/goods/search?bidType=1"
        button_name_pattern = re.compile(
            r"Create listings|Bid|Ask|List|上架|出价|입찰",
            re.IGNORECASE,
        )
        self.log(f"[POIZON SELLER] Searching for '{serial_number}'...")

        try:
            self.log(f"[POIZON SELLER] Opening search page: {target_url}")
            self.page.goto(target_url, wait_until="domcontentloaded", timeout=30000)

            search_input = self.page.locator("input#keyword[type=\"text\"]")
            search_input.wait_for(state="visible", timeout=10000)
            search_input.click()
            search_input.press("Control+A")
            search_input.press("Backspace")
            search_input.type(serial_number, delay=80)
            search_input.dispatch_event("input")
            search_input.dispatch_event("change")
            search_input.press("Tab")
            self.log(f"[POIZON SELLER] Search input: {serial_number}")

            self._click_search_button()
            self.log("[POIZON SELLER] Clicked 'Search & List'.")

            result_rows = self.page.locator(".ant-table-tbody > tr:not(.ant-table-measure-row)")
            result_rows.first.wait_for(state="visible", timeout=15000)
            self.log("[POIZON SELLER] Waiting for refreshed search results...")

            matched_row = None
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    current_rows = result_rows.filter(has_text=serial_number)
                    if current_rows.count() > 0:
                        matched_row = current_rows.first
                        row_text = (matched_row.text_content(timeout=1000) or "").strip()
                        self.log(f"[POIZON SELLER] Search result matched: {row_text[:120]}")
                        break
                except Exception:
                    pass
                self.page.wait_for_timeout(500)

            target_button = None
            try:
                if matched_row is not None:
                    target_button = matched_row.get_by_role(
                        "button", name=re.compile(r"입찰|Create listings|Bid|Ask|上架", re.IGNORECASE)
                    ).first
            except Exception:
                target_button = None

            if target_button is None:
                self.log("[POIZON SELLER] No exact row match found. Falling back to first result.")
                first_row = result_rows.first
                target_button = first_row.get_by_role(
                    "button", name=re.compile(r"입찰|Create listings|Bid|Ask|上架", re.IGNORECASE)
                ).first

            target_button.wait_for(state="visible", timeout=10000)
            target_button.click()
            self.log("[POIZON SELLER] Clicked the matched product bid/listing button.")
            self.page.wait_for_timeout(1500)

            return self._perform_scraping_logic()

        except Exception as e:
            self.log(f"[POIZON SELLER] Detail scrape failed: {e}")
            return {}, ""

    def scrape_direct_url(self, url):
        if not self.page:
            self.log("[POIZON SELLER] Browser has not been started.")
            return {}, ""

        self.log(f"[POIZON SELLER] Opening direct URL: {url}")
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            self.page.wait_for_timeout(1500)
            return self._perform_scraping_logic()
        except Exception as e:
            self.log(f"[POIZON SELLER] Direct URL scrape failed: {e}")
            return {}, ""

    def close(self):
        if self.context:
            self.context.close()
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()

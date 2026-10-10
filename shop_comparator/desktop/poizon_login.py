import json
import time

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


def login_and_save_poizon_cookies(email=None, password=None, log_callback=print):
    """Open POIZON in Chrome, wait for manual login, and persist cookies."""
    options = webdriver.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    log_callback("[POIZON] Starting browser for login.")
    driver = None
    try:
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
        driver.get("https://kr.poizon.com/")
        log_callback("[POIZON] Main page loaded.")

        wait = WebDriverWait(driver, 20)
        try:
            login_button_selector = 'div[data-navbar-user-icon="1"]'
            login_button = wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, login_button_selector))
            )
            driver.execute_script("arguments[0].click();", login_button)
            log_callback("[POIZON] Login popup opened. Complete login in the browser.")
        except Exception as exc:
            log_callback(f"[POIZON][WARN] Could not open login popup automatically: {exc}")

        timeout = 300
        start_time = time.time()
        last_cookie_count = 0

        while time.time() - start_time < timeout:
            cookies = driver.get_cookies()
            cookie_dict = {cookie["name"]: cookie["value"] for cookie in cookies}

            if len(cookies) != last_cookie_count:
                last_cookie_count = len(cookies)
                log_callback(f"[POIZON] Cookie count: {last_cookie_count}")

            if "du_token" in cookie_dict or len(cookies) >= 15:
                cookie_file = "poizon_cookies.json"
                with open(cookie_file, "w", encoding="utf-8") as f:
                    json.dump(cookie_dict, f, indent=4)
                log_callback(f"[POIZON] Cookies saved: {cookie_file}")
                return True

            time.sleep(2)

        log_callback("[POIZON][WARN] Login wait timed out.")
        return False

    except Exception as exc:
        log_callback(f"[POIZON][ERROR] Login failed: {exc}")
        return False
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    email = input("POIZON email: ").strip()
    password = input("POIZON password: ").strip()
    if not email or not password:
        raise SystemExit("Email and password are required.")
    login_and_save_poizon_cookies(email, password)

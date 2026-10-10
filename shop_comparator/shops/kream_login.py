from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import time, json, os
from shop_comparator.shops.device_id import get_device_id
from shop_comparator.core.utils import get_persistent_path

def _log(log_callback, msg):
    if log_callback:
        log_callback(msg)

def _extract_token_and_device_id_from_localstorage(driver):
    # access_token 추출 시도 (여러 키 확인)
    access_token = None
    
    try:
        # 1. 명시적 키 확인
        token_candidates = driver.execute_script("""
            const candidates = {};
            const keys = ['_token.local.p-2', 'accessToken', 'KREAM_APP_AUTH', 'auth', 'token'];
            keys.forEach(k => {
                const v = localStorage.getItem(k);
                if(v) candidates[k] = v;
            });
            // sessionStorage도 확인
            const ssToken = sessionStorage.getItem('accessToken');
            if(ssToken) candidates['sessionStorage_accessToken'] = ssToken;
            
            return candidates;
        """)

        # 우선순위에 따라 토큰 파싱
        # _token.local.p-2 는 보통 JSON {"access_token": "..."} 형태
        if '_token.local.p-2' in token_candidates:
            raw = token_candidates['_token.local.p-2']
            try:
                obj = json.loads(raw)
                access_token = obj.get("access_token") or obj.get("token") or obj.get("accessToken")
            except:
                access_token = raw # JSON 아니면 원본 사용

        # 아직 없으면 accessToken 키 확인 (보통 평문)
        if not access_token and 'accessToken' in token_candidates:
            access_token = token_candidates['accessToken']
            
        # KREAM_APP_AUTH (JSON)
        if not access_token and 'KREAM_APP_AUTH' in token_candidates:
             raw = token_candidates['KREAM_APP_AUTH']
             try:
                obj = json.loads(raw)
                access_token = obj.get("access_token") or obj.get("token")
             except:
                access_token = raw

        # 기타 키들
        if not access_token:
            for k, v in token_candidates.items():
                if k in ['auth', 'token', 'sessionStorage_accessToken'] and v:
                    # 값이 JSON일 수 있으므로 체크
                    if v.strip().startswith('{'):
                         try:
                            obj = json.loads(v)
                            access_token = obj.get("access_token") or obj.get("token") or obj.get("accessToken")
                         except:
                            access_token = v
                    else:
                        access_token = v
                    if access_token: break

        # 2. 그래도 없으면 localStorage 전체 뒤져서 JWT스러운거 찾기
        if not access_token:
            _log(None, "[KREAM LOGIN] 특정 키에서 토큰을 찾지 못해 전체 스캔을 시도합니다.")
            all_ls = driver.execute_script("""
                const ls = {};
                for (let i=0; i<localStorage.length; i++){
                    const k = localStorage.key(i);
                    ls[k] = localStorage.getItem(k);
                }
                return ls;
            """)
            for k, v in all_ls.items():
                if isinstance(v, str) and v.startswith("eyJ") and len(v) > 100: # JWT prefix
                    access_token = v
                    _log(None, f"[KREAM LOGIN] 발견된 잠재적 토큰 키: {k}")
                    break
                # 값 안에 access_token 키가 있는 JSON일 경우
                if "access_token" in v and v.strip().startswith("{"):
                    try:
                        obj = json.loads(v)
                        if obj.get("access_token"):
                            access_token = obj.get("access_token")
                            _log(None, f"[KREAM LOGIN] JSON 내 access_token 발견 키: {k}")
                            break
                    except: pass

    except Exception as e:
        _log(None, f"[KREAM LOGIN] 토큰 추출 중 스크립트 오류: {e}")

    # device_id 추출 (device_id.py 모듈 사용 및 포맷팅)
    device_id = f"web;{get_device_id()}"

    # sentry-trace 및 baggage 추출 시도
    sentry_trace = None
    baggage = None
    try:
        all_local_storage = driver.execute_script("""
            const ls = {};
            for (let i=0; i<localStorage.length; i++){
                const k = localStorage.key(i);
                ls[k] = localStorage.getItem(k);
            }
            return ls;
        """)
        
        for key, value in all_local_storage.items():
            if "sentry-trace" in key.lower() and isinstance(value, str) and len(value) > 10:
                sentry_trace = value
            if "baggage" in key.lower() and isinstance(value, str) and len(value) > 10:
                baggage = value
            if "ab.storage.serverConfig" in key and isinstance(value, str):
                try:
                    server_config = json.loads(value)
                    if "v" in server_config and "sentry-trace" in server_config["v"]:
                        pass
                except Exception: pass

    except Exception as e:
        _log(None, f"[KREAM LOGIN] sentry/baggage 추출 중 오류 발생: {e}")

    return access_token, device_id, sentry_trace, baggage

def login_and_save_kream_cookies(username, password, cookie_file_path, log_callback=None):
    """
    Kream에 로그인하고 쿠키와 access/refresh 토큰, device_id, sentry 헤더를 파일로 저장합니다.
    """
    _log(log_callback, f"[KREAM LOGIN] 로그인 시도 중: {username}")
    
    driver = None
    try:
        options = webdriver.ChromeOptions()
        options.add_argument("--start-maximized")
        # 봇 탐지 방지 옵션
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)

        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
        
        # 봇 탐지 우회 스크립트
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                })
            """
        })

        driver.get("https://kream.co.kr/login")
        wait = WebDriverWait(driver, 20)

        # 페이지 로딩 및 Hydration 대기 (잠시 대기)
        time.sleep(2)

        # 1. 이메일 입력 (재시도 로직 포함)
        email_input = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='email']")))
        email_input.click()
        time.sleep(0.2)
        
        # 확실히 지우기 (Ctrl+A -> Del)
        email_input.send_keys(Keys.CONTROL, 'a')
        email_input.send_keys(Keys.BACK_SPACE)
        time.sleep(0.2)
        email_input.send_keys(username)
        time.sleep(0.5)

        # 값 검증 및 재입력
        current_val = email_input.get_attribute("value")
        if current_val != username:
            _log(log_callback, "[KREAM LOGIN] 이메일 입력 확인 실패, 재시도...")
            email_input.clear() # clear()도 다시 시도
            email_input.send_keys(username)
            time.sleep(0.5)

        # 2. 비밀번호 입력
        pw_input = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
        pw_input.click()
        pw_input.send_keys(Keys.CONTROL, 'a')
        pw_input.send_keys(Keys.BACK_SPACE)
        time.sleep(0.2)
        pw_input.send_keys(password)
        time.sleep(0.5)

        # 3. 로그인 버튼 상태 확인 및 클릭
        login_btn = driver.find_element(By.CSS_SELECTOR, "button.btn.full.solid")
        
        # 버튼 활성화 대기 (class에 disabled가 빠지거나 style이 바뀜)
        # Kream은 보통 disabled 속성을 쓰거나 색상이 바뀜.
        # 안전하게 1초 대기 후 클릭 시도
        time.sleep(1)
        
        if not login_btn.is_enabled():
             _log(log_callback, "[WARN] 로그인 버튼이 비활성화 상태입니다. 입력값을 확인해주세요.")
             # 그래도 일단 클릭 시도해봄 (JavaScript로 제어될 수 있음)

        login_btn.click()
        _log(log_callback, "[KREAM LOGIN] 로그인 버튼 클릭. 결과 대기 중...")

        # 4. 결과 판별 (URL 변경 or 에러 메시지)
        def check_login_status(d):
            current_url = d.current_url
            if "kream.co.kr" in current_url and "/login" not in current_url:
                return "success"
            
            # 에러 메시지 (input_error 클래스 등)
            try:
                errs = d.find_elements(By.CSS_SELECTOR, ".input_error")
                for err in errs:
                    if err.is_displayed() and err.text:
                        return f"failed: {err.text}"
                # 얼럿(Toast) 메시지 확인
                toasts = d.find_elements(By.CSS_SELECTOR, ".toast-message") # 가상의 선택자
                if toasts and toasts[0].is_displayed():
                     return f"failed: {toasts[0].text}"
            except: pass
            
            return False

        try:
            result = WebDriverWait(driver, 30).until(check_login_status)
            
            if isinstance(result, str) and result.startswith("failed"):
                _log(log_callback, f"[KREAM LOGIN] 로그인 실패 감지: {result}")
                return False
            
            _log(log_callback, "[KREAM LOGIN] 메인 페이지 진입 확인. 데이터 저장 시작.")

        except Exception:
            _log(log_callback, "[KREAM LOGIN] 로그인 타임아웃. URL이 변경되지 않았습니다.")
            return False

        # --- 데이터 저장 (성공 시) ---
        cookies = driver.get_cookies()
        os.makedirs(os.path.dirname(cookie_file_path) or ".", exist_ok=True)
        with open(cookie_file_path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        _log(log_callback, f"[KREAM LOGIN] 쿠키 저장 완료: {cookie_file_path} (개수 {len(cookies)})")

        token_file_path = os.path.splitext(cookie_file_path)[0] + "_token.json"
        user_agent = driver.execute_script("return navigator.userAgent;")

        access_token, device_id, sentry_trace, baggage = _extract_token_and_device_id_from_localstorage(driver)
        refresh_token = None
        for c in cookies:
            if c.get("name") in ("_refresh_token.local.p-2", "_refresh_token.local.p-1"):
                refresh_token = c.get("value")
                break
        
        if not access_token:
            _log(log_callback, "[KREAM LOGIN] 주의: access token을 추출하지 못했습니다.")

        token_data = {
            "saved_at": int(time.time()),
            "user_agent": user_agent,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "device_id": device_id,
            "sentry_trace": sentry_trace,
            "baggage": baggage,
        }
        with open(token_file_path, "w", encoding="utf-8") as f:
            json.dump(token_data, f, ensure_ascii=False, indent=2)
        _log(log_callback, f"[KREAM LOGIN] 토큰/Device ID 저장 완료: {token_file_path}")
        
        return True

    except Exception as e:
        _log(log_callback, f"[KREAM LOGIN] 오류 발생: {e}")
        return False
    finally:
        if driver:
            driver.quit()

if __name__ == "__main__":
    from shop_comparator.core import config
    # 테스트용
    user_id = input("KREAM email: ").strip()
    user_pw = input("KREAM password: ").strip()
    if not user_id or not user_pw:
        raise SystemExit("Email and password are required.")

    persistent_cookie_path = get_persistent_path(config.KREAM_COOKIES_FILE)
    login_and_save_kream_cookies(user_id, user_pw, persistent_cookie_path, log_callback=print)

# 수집 방식: API 우선, 브라우저 컨텍스트 경유

## 1. 결론

두 쇼핑몰 모두 **쇼핑몰이 자기 화면에서 쓰는 JSON API를 호출**해 수집한다. HTML 파싱은 무신사 품번(모델번호) 한 가지에만 쓴다. 다만 요청을 파이썬 HTTP 클라이언트로 직접 보내지 않고, **쇼핑몰 페이지가 열린 Chrome 안에서 `fetch()`로** 보낸다(`browser_transport.BrowserSession`).

| 대상 | 방식 | 근거 |
|---|---|---|
| 무신사 랭킹·카테고리·브랜드 목록, 상품 상세, 옵션, 재고, 장바구니, 주문서, 쿠폰 | Chrome 안 `fetch()` → JSON API | 직접 HTTP는 403, Chrome 안에서는 200 |
| 무신사 품번 | Chrome 안 `fetch()` → 상품 페이지 HTML에서 정규식 추출 | 품번을 주는 JSON API를 쓰지 않는 기존 구현 유지 |
| KREAM 검색, 사이즈별 시세, 체결 내역 | Chrome 안 `fetch()` → JSON API | 직접 HTTP와 헤드리스 Chrome은 500, 일반 Chrome 창은 200 |
| 로그인 | 보이는 Chrome 창 조작(Selenium) | 로그인 화면은 API로 대체하지 않음 |
| POIZON | Playwright로 셀러 센터 화면 조작(기존 모듈) | 기존 구현 유지 |

## 2. 2026-10-09 재현 결과

같은 PC, 같은 네트워크에서 방식만 바꿔 요청했다.

### 무신사 (`/products/1527076`)

| 방식 | 옵션 API | 상세 API | 상품 페이지 |
|---|---|---|---|
| `requests` + Chrome User-Agent | 403 (HTML 5,485바이트) | 403 | 403 |
| 헤드리스 Chrome 안 `fetch()` (로그인 없음) | 200 | 200 | 200 |

헤드리스 Chrome에서 옵션 7개(225~255), 재고 API 기준 구매 가능 4개(235, 240, 245, 250)를 받았다. 이전 관측과 같다.

### KREAM (`/products/74130`)

| 방식 | 결과 |
|---|---|
| `requests` + Chrome User-Agent | 500, 본문 0바이트 (상품 페이지와 API 모두) |
| Selenium 헤드리스 Chrome | 문서 응답 500 |
| Selenium 일반 창 Chrome (추가 옵션 없음, `navigator.webdriver = true`) | 문서 응답 200, 상품 화면 정상 |
| 사람이 쓰는 일반 브라우저 | 200, 상품명·모델번호·사이즈표 정상 표시 |

판단: 이 500은 사이트 장애도, 토큰 만료도, IP 차단도 아니다. **헤드리스 브라우저와 비브라우저 클라이언트에 대한 응답**이다. 같은 시각 같은 PC에서 일반 창은 200이었기 때문이다. 로그인 여부와 무관하게 재현됐다.

대응: KREAM 작업은 일반 Chrome 창(최소화)으로 실행한다. 자동화 탐지를 숨기는 옵션은 쓰지 않는다(`navigator.webdriver`가 true인 상태 그대로 200을 받는다).

### KREAM 로그인 화면

`https://kream.co.kr/login` 에는 이제 "네이버로 로그인", "Apple로 로그인", "휴대폰으로 로그인" 버튼만 있고 입력란이 없다(`input` 0개). 이메일 로그인은 `https://kream.co.kr/login/email` 로 옮겨졌고 `input[type=email]`, `input[type=password]`가 있다. 기존 `shop_comparator/shops/kream_login.py`는 `/login`에서 `input[type='email']`을 20초 기다리다 실패한다.

## 3. 방식 선택 이유

| 후보 | 채택 여부 | 이유 |
|---|---|---|
| 직접 HTTP (`requests`, `curl`) | 미채택 | 두 쇼핑몰 모두 지금은 거절한다. 헤더를 흉내 내 통과시키는 것은 유지보수와 정책 양쪽에서 부적절하다. |
| Chrome 안에서 API 호출 | **채택** | 사이트가 실제로 쓰는 요청 경로다. 쿠키·브라우저 헤더를 Chrome이 관리하므로 코드에 고정값이 줄고, JSON이라 화면 구조 변경에 덜 민감하다. |
| Selenium/Playwright로 화면 조작 | 로그인·POIZON에만 | 사이즈별 가격을 화면 클릭으로 읽으면 느리고 화면 변경에 약하다. |
| HTML 파싱(BeautifulSoup) | 품번 추출에만 | 가격·재고는 JSON API가 더 정확하다. |

중복 구현 정리

- 웹 경로의 KREAM 시세는 `shop_comparator/shops/kream_api.py` 하나만 쓴다. `get_kream_price6.run_logic`(curl 직접 호출)과 `rotate_module.fetch_sales`(requests 직접 호출)는 지금 환경에서 500을 받으므로 웹에서는 쓰지 않는다. 두 파일은 데스크톱 GUI가 참조하므로 남겨 두었다.
- 이전 웹 구현의 `browser_login.py`(수동 로그인 전용)는 `shop_comparator/shops/site_login.py`로 대체하고 삭제했다.

## 4. KREAM 요청 헤더

`shop_comparator/shops/kream_crawler.py`에는 API 버전(`52`), 웹 빌드 버전(`25.15.7`), 요청 시크릿이 상수로 들어 있다. 2026-10-09 현재 사이트가 쓰는 값은 `64`, `26.13.5`이고 상수 값으로는 403이 난다. 상품 페이지는 서버 렌더링이라 열기만 해서는 API 요청이 없으므로, 브라우저 어댑터는 먼저 페이지에 들어 있는 사이트 설정(`__NUXT__.config`)과 `webDid` 쿠키에서 값을 읽는다. 또한 KREAM 페이지가 실제로 보내는 요청을 Chrome 성능 로그에서 읽어 `x-kream-api-version`, `x-kream-web-build-version`, `x-kream-web-request-secret`, `x-kream-device-id`, `authorization`을 **관측값 우선**으로 덮어쓴다. 관측하지 못한 항목만 상수를 쓴다. 관측값은 `cookies_token.json`의 `request_headers`에도 저장해 다음 실행에 쓴다(`authorization` 제외).

KREAM API는 쿠키 포함 요청을 CORS에서 거절하므로 `credentials: 'omit'`으로 보내고, 인증이 필요한 체결 내역 API에만 `Authorization` 헤더를 붙인다. 시세와 검색은 로그인 없이 응답한다. 저장 파일의 오래된 토큰은 웹 경로에서 보내지 않는다.

## 5. 요청 예절과 차단 대응

- 요청 사이 0.3~1초 간격을 둔다. 병렬 요청은 하지 않는다(쇼핑몰당 Chrome 하나).
- 401(인증), 403(차단), 429(한도)를 받으면 그 작업을 즉시 멈춘다. 자동 재시도, 프록시 순환, 인증 화면 우회는 하지 않는다.
- 회원가 조회가 5회 연속 실패하면 작업을 멈춘다.
- 500 등 그 밖의 오류는 해당 상품만 실패로 기록하고 다음 상품으로 넘어간다. 이전에 저장한 값은 지우지 않는다.

## 6. 남은 위험

- 쇼핑몰이 API 경로나 응답 구조를 바꾸면 수집이 실패한다. 이 경우 `schema` 오류로 기록되고 가격 칸은 비워 둔다.
- KREAM이 일반 창의 자동화 브라우저도 거절하게 되면 KREAM 수집은 동작하지 않는다. 그때는 수동 확인 외에 방법이 없다.
- 헤드리스 Chrome의 무신사 접근 허용도 쇼핑몰 정책에 달려 있다. `SHOP_MUSINSA_HEADLESS=0`으로 일반 창 실행으로 바꿀 수 있다.

# 기존 GUI → 웹 기능 대응표

기준 코드: `shop_comparator/desktop/gui.py`(Tkinter, 4,637줄)와 연결 모듈. 줄 번호는 `shop_comparator/desktop/gui.py` 기준이다.
"웹(이전)"은 이 작업을 시작하기 전 웹 구현 상태이고, "웹(현재)"와 "검증"은 이 문서 마지막 갱신 시점의 상태다.

검증 표기 (2026-10-09 기준, 상세는 [live-verification.md](live-verification.md), [testing.md](testing.md))

- 실사이트: 실제 무신사/KREAM 응답으로 확인
- 모의: 단위/통합 테스트(모의 데이터)로만 확인
- 화면 확인: 웹 화면에 요소가 있고 동작하는 것만 확인
- 미검증: 코드는 이식했으나 실행 확인을 하지 못함(사유 기재)

## 1. 메인 창

### 1.1 무신사 영역 (`build_widgets` 408~422)

| ID | GUI 요소 | 연결 함수 | 웹(이전) | 웹(현재) | 검증 |
|---|---|---|---|---|---|
| M1 | 아이디·비밀번호 입력 | `musinsa_id`, `musinsa_pw` | 없음 | 로그인 탭 무신사 카드 | 화면 확인 |
| M2 | [쿠키 얻기] 자동 로그인 | `on_click_get_musinsa_cookie`(1031) → `crawler.get_cookie_with_selenium` → `database.save_cookie` | 없음(수동 로그인만) | `POST /api/login/musinsa` → `site_login.login_musinsa` | 모의(자동 입력 제출은 실계정 미검증) · 수동 로그인은 실사이트 |
| M3 | TopN 입력 | `entry_topn` | 수집 개수(최대 100) | 수집 탭 "상품 수" | 실사이트 |
| M4 | [무신사 크롤링] 랭킹/카테고리 수집 | `on_click_musinsa_collect`(1049) → `_run_musinsa_crawl_task`(1339) → `crawler.run_ranking_crawl` / `run_full_crawl` → `database.save_products` | 통합 비교의 일부로만 실행 | 수집 탭 [무신사만 수집] `POST /api/jobs/musinsa` | 실사이트(직접 입력·랭킹) · 카테고리는 모의 |
| M5 | [브랜드 필터링] | `open_brand_selection_window`(1069) | 없음 | 수집 탭 "브랜드" 방식 | 모의(브랜드 목록 711개 표시 확인, 브랜드 수집 실행은 미검증) |
| M6 | 저장된 무신사 세션 복구 | `load_initial_data`(646) → `database.load_auth_session` | 있음 | 전용 Chrome 프로필 + 상태 표시 | 실사이트 |

### 1.2 KREAM 영역 (424~454)

| ID | GUI 요소 | 연결 함수 | 웹(이전) | 웹(현재) | 검증 |
|---|---|---|---|---|---|
| K1 | 아이디·비밀번호 입력 | `kream_id`, `kream_pw` | 없음 | 로그인 탭 KREAM 카드 | 화면 확인 |
| K2 | [KREAM 로그인] 자동 로그인 | `on_click_kream_login`(1384) → `kream_login.login_and_save_kream_cookies` | 없음(수동 로그인만) | `POST /api/login/kream` → `site_login.login_kream` | 모의(자동 입력 제출은 실계정 미검증) · 수동 로그인은 실사이트 |
| K3 | 로그인 상태 라벨 | `kream_status_label` | 없음 | 로그인 탭 상태 배지 + 인증 확인 | 실사이트 |
| K4 | 토큰 만료 시 자동 재로그인 | `_execute_kream_api`(370), `_auto_login_kream`(1433) | 없음 | 재로그인 안내 + 선택 시 메모리 보관 계정으로 재로그인 | 모의 |
| K5 | [크림 상품번호 추출] 검색·매칭 | `on_click_extract_kream_ids`(1404) → `_get_or_update_db_dataframe`(2206) → `kream_crawler.search_product` → `database.cache_kream_search`, 매칭 CSV, 블랙리스트 CSV | 통합 비교의 일부(자동 매칭만) | 수집 탭 [KREAM 매칭] `POST /api/jobs/match` | 실사이트(검색 동작, 검증기 미채택) · 수동 지정은 실사이트 |
| K6 | [크림 가격 추출] 즉시구매/즉시판매가 | `on_click_extract_kream_prices`(1422) → `_run_extract_kream_prices_task`(1460) → `get_kream_price6.run_logic` → `update_kream_prices`(695) | 즉시구매가만 | 수집 탭 [KREAM 가격 수집] `POST /api/jobs/kream` | 실사이트 |
| K7 | 최소 거래건수 / 최소 한달 거래수 + [크림 필터링] | `on_click_kream_filter`(1611) → `rotate_module.fetch_sales` → `database.update_product_kream_transactions` | 없음 | 수집 탭 [거래량 필터] `POST /api/jobs/filter` | 실사이트 |
| K8 | [무신사+크림 한번에] | `on_click_full_crawl`(1722) | [가격 비교 시작] | 수집 탭 [전체 순차 실행] `POST /api/jobs/full` | 실사이트(무신사 단계) · KREAM 단계는 개별 실행으로 실사이트 |
| K9 | 환경변수 `KREAM_BEARER` 토큰 사용 | `load_initial_data`(657) | 있음 | 유지 | 유지(웹 경로에서는 저장 토큰을 보내지 않음) |

### 1.3 POIZON 영역 (460~480)

| ID | GUI 요소 | 연결 함수 | 웹(이전) | 웹(현재) | 검증 |
|---|---|---|---|---|---|
| P1 | 아이디·비밀번호 입력 | `poizon_id`, `poizon_pw` | 없음 | 로그인 탭 POIZON 카드 | 화면 확인 |
| P2 | [로그인 & 브라우저 시작] | `on_click_poizon_login_auto`(954) → `_process_poizon_task('LOGIN')`(128) → `PoizonSellerCrawler.login` | 없음 | `POST /api/login/poizon` | 미검증(계정 없음) |
| P3 | [포이즌 가격 추출 (Seller)] | `on_click_extract_poizon_prices`(988) → `_process_poizon_task('CRAWL_LIST')` → `update_poizon_prices`(861) | 없음 | 수집 탭 [POIZON 가격 수집] `POST /api/jobs/poizon` | 미검증(계정 없음) |
| P4 | [무신사+포이즌 통합] | `on_click_full_poizon_crawl`(1736) | 없음 | 수집 탭 [무신사+POIZON 순차] | 미검증(계정 없음) |
| P5 | POIZON 캐시/블랙리스트 CSV | `_process_poizon_task`(185~247, 350~366) | 없음 | 그대로 재사용 | 미검증 |

### 1.4 엑셀 작업 영역 (482~502)

| ID | GUI 요소 | 연결 함수 | 웹(이전) | 웹(현재) | 검증 |
|---|---|---|---|---|---|
| E1 | 마진율·수량·입찰기한 + [엑셀 파일 생성] (KREAM 입찰 양식) | `on_click_generate_excel`(2473) → `_generate_excel_task`(2477) → `create_excel_file`(2675), 템플릿 `k-c46vwlba.xlsx` | 없음 | 내보내기 탭 [입찰 엑셀] `GET /api/export/bid.xlsx` | 실사이트 |
| E2 | [비교 CSV 저장] | `on_click_save_comparison_csv`(2801) → `_save_comparison_csv_task`(2805) | 없음 | 내보내기 탭 [비교 CSV] `GET /api/export/comparison.csv` | 실사이트 |
| E3 | (웹 추가) 사이즈별 비교 엑셀 | 이전 웹 `export_workbook` | 있음 | 내보내기 탭 [비교 엑셀] `GET /api/export/comparison.xlsx` | 실사이트 |
| E4 | [리스탁 모니터링] | `open_monitoring_window`(2972) | 없음 | 모니터링 탭 "재입고" | 실사이트 |
| E5 | [가격변동 모니터링] | `open_price_monitoring_window`(2978) | 없음 | 모니터링 탭 "가격변동" | 모의 |
| E6 | [구매제한 모니터링] | `open_purchase_limit_monitoring_window`(2984) | 없음 | 모니터링 탭 "구매제한" | 실사이트 |

### 1.5 디스코드 알림 설정 (505~513)

| ID | GUI 요소 | 연결 함수 | 웹(이전) | 웹(현재) | 검증 |
|---|---|---|---|---|---|
| D1 | 웹훅 URL | `discord_webhook_url_entry` → `webhook.send_discord_notification` | 없음 | 설정 탭(응답에는 마스킹) | 모의 |
| D2 | 알림 마진 + 수익 사이즈 알림 | `_get_discord_alert_margin`(793), `_send_kream_margin_notification`(799) | 없음 | 설정 탭 "알림 마진", KREAM 가격 수집 시 발송 | 모의(실제 발송 미검증) |

### 1.6 이벤트 트리거 자동화 (515~551)

| ID | GUI 요소 | 연결 함수 | 웹(이전) | 웹(현재) | 검증 |
|---|---|---|---|---|---|
| A1 | 낮 감시 주기 / 밤 전체 갱신 주기 | `_get_auto_event_interval`(1810, 최소 30초) | 없음 | 모니터링 탭 "자동화" | 모의 |
| A2 | [자동 감지 시작] (시간대별 모드 자동 전환) | `on_click_start_auto_event_mode`(2181) → `_auto_event_loop`(2123), `_get_auto_mode`(1821, 0~9시 밤) | 없음 | `POST /api/auto/start` mode=auto | 미검증(실행 안 함) |
| A3 | [낮 모드 시작] 가격 하락 감지 시 KREAM 비교 | `on_click_start_day_event_mode`(2188) → `_run_auto_event_cycle`(2066) | 없음 | mode=day_event | 미검증(실행 안 함) |
| A4 | [밤 모드 시작] 전체 갱신 | `on_click_start_night_full_mode`(2195) → `_run_auto_full_cycle`(2110) | 없음 | mode=night_full | 미검증(실행 안 함) |
| A5 | [자동 이벤트 모드 중지] + 상태 라벨 | `on_click_stop_auto_event_mode`(2202) | 없음 | `POST /api/auto/stop` + 상태 표시 | 미검증(실행 안 함) |
| A6 | 마지막 관측가 저장(가격 하락 기준) | `_persist_last_observed_price`(1854) → `database.update_last_observed_price` | 없음 | 동일 DB 컬럼 사용 | 모의 |

### 1.7 카테고리 선택 (553~556, `setup_category_checkboxes` 609)

| ID | GUI 요소 | 연결 함수 | 웹(이전) | 웹(현재) | 검증 |
|---|---|---|---|---|---|
| C1 | 카테고리 13종 다중 선택 | `self.categories`(98) | 7종, 단일 선택 | 13종 다중 선택 | 모의(검증) |
| C2 | 랭킹 3종(신발, 부티크 지갑/가방, 부티크 악세서리) 다중 선택 | `self.ranking_categories`(103) | 신발 랭킹만 | 3종 다중 선택 | 실사이트(신발 랭킹) |

### 1.8 상품 목록 창 (`open_product_list_window` 570)

| ID | GUI 요소 | 연결 함수 | 웹(이전) | 웹(현재) | 검증 |
|---|---|---|---|---|---|
| L1 | 상품 트리(랭킹·상품명·브랜드·사이즈·무신사 최종가·즉시구매가·즉시판매가·차액) | `setup_treeview`(633), `populate_tree`(679), `update_kream_prices`(695) | 사이즈별 평면 표(판매가 없음) | 상품 탭: 상품 행 + 사이즈별 하위 행 | 실사이트 |
| L2 | [선택 상세보기] | `open_selected_detail`(2933) → `DetailWindow`(4540) | 없음 | 상품 행 [상세] 패널 | 실사이트 |
| L3 | [상품 페이지 열기] | `open_selected_link`(2787) | 무신사/KREAM 링크 | 무신사/KREAM/POIZON 링크 | 실사이트 |
| L4 | 저장된 상품 복구 | `load_initial_data`(674) → `database.load_products` | 없음(메모리만) | DB에서 상품·사이즈·매칭·가격 복구 | 실사이트 |
| L5 | POIZON 수집 시 열 제목 전환 | `_change_tree_headings`(851) | 없음 | POIZON 열을 별도로 표시 | 미검증 |
| L6 | (웹 추가) 상품 직접 입력, 선택 상품만 갱신, 목록에서 제외 | 이전 웹 `validate_settings` | 직접 입력만 | 상품 탭 체크박스 + [선택 갱신]/[제외] | 실사이트 |
| L7 | (웹 추가) 매칭 확인·수동 지정·해제 | GUI는 CSV 직접 수정에 의존 | 입력 시 URL 쌍 지정만 | 매칭 탭 | 실사이트 |

### 1.9 진행 로그 (564~568)

| ID | GUI 요소 | 연결 함수 | 웹(이전) | 웹(현재) | 검증 |
|---|---|---|---|---|---|
| G1 | 진행 로그 창 | `log`(643) → `utils.append_log` | 최근 300줄(메모리) | 로그 탭: 작업·쇼핑몰·단계·오류 필터, 파일 로그 | 실사이트 |

## 2. 브랜드 선택 창 (`open_brand_selection_window` 1069)

| ID | GUI 요소 | 연결 함수 | 웹(이전) | 웹(현재) | 검증 |
|---|---|---|---|---|---|
| B1 | 브랜드 체크박스 목록(매칭 CSV 기반) | `find_csv_file("kream_overlapping_brands")` → `utils.get_musinsa_brands_from_csv` | 없음 | `GET /api/brands` + 검색 | 실사이트 |
| B2 | 브랜드당 수집 개수 | `count_entry` | 없음 | "브랜드당 수집 개수" | 모의 |
| B3 | [선택한 브랜드 크롤링 시작] | `start_brand_crawling`(1153) → `_run_brand_crawl_task`(1177) → `crawler.get_products_by_brand` | 없음 | [무신사만 수집] 방식=브랜드 | 미검증(실행 안 함) |
| B4 | [선택 브랜드 무신사+크림] | `start_brand_full_crawl`(1239) | 없음 | [전체 순차 실행] 방식=브랜드 | 미검증(실행 안 함) |
| B5 | [선택 브랜드 무신사+포이즌] | `start_brand_full_poizon_crawl`(1254) | 없음 | [무신사+POIZON 순차] 방식=브랜드 | 미검증 |
| B6 | [선택 브랜드 이벤트 자동 시작] | `start_brand_auto_event_mode`(1267) | 없음 | 자동화 "감시 대상=선택 브랜드" | 미검증 |
| B7 | [자동 중지] | `on_click_stop_auto_crawl`(1802) | 없음 | 자동화 [중지] | 미검증 |

## 3. 모니터링 창

### 3.1 재입고 (`MonitoringWindow` 3093, 파일 끝의 `_patched_monitoring_*`가 실제 동작)

| ID | GUI 요소 | 연결 함수 | 웹(이전) | 웹(현재) | 검증 |
|---|---|---|---|---|---|
| R1 | 상품 ID [추가] / [선택 삭제] | `add_item`(3179), `delete_item`(3228) | 없음 | 모니터링 탭 | 실사이트 |
| R2 | [현재 크롤링 상품 모니터링] | `_patched_monitoring_add_current_products_v2`(4467) | 없음 | [현재 상품 목록 추가] | 모의 |
| R3 | 주기(기본 180초) [모니터링 시작]/[중지] | `start_monitoring`(3362), `stop_monitoring`(3392) | 없음 | 동일 | 실사이트 |
| R4 | 재고 있음/품절 사이즈 표시, 재입고·부분 재입고 감지, 디스코드 알림 | `_patched_monitoring_loop`(4367) → `crawler.extract_stock_size_sets` | 없음 | `monitoring.restock_check` | 실사이트(재고 조회) · 재입고 감지는 모의 |

### 3.2 가격변동 (`PriceMonitoringWindow` 3523)

| ID | GUI 요소 | 연결 함수 | 웹(이전) | 웹(현재) | 검증 |
|---|---|---|---|---|---|
| V1 | 상품 ID [추가]/[선택 삭제]/[현재 크롤링 상품] | `add_item`(3587), `delete_item`(3652), `add_current_products`(3662) | 없음 | 모니터링 탭 | 모의 |
| V2 | 주기(기본 300초), 최초가·현재가·상태, 가격 하락 시 알림 | `_monitoring_loop`(3738) → `crawler.process_one`, `get_price_notification_message`(3803) | 없음 | `monitoring.price_check` | 모의 |

### 3.3 구매제한 (`PurchaseLimitMonitoringWindow` 3847)

| ID | GUI 요소 | 연결 함수 | 웹(이전) | 웹(현재) | 검증 |
|---|---|---|---|---|---|
| U1 | 상품 ID [추가]/[선택 삭제]/[현재 크롤링 상품] | `add_item`(3908), `_patched_purchase_limit_*`(4193~4285) | 없음 | 모니터링 탭 | 실사이트 |
| U2 | 주기(기본 240초), 구매 제한 해제 감지 + 알림 | `_purchase_limit_monitoring_loop`(4039) → `crawler.fetch_purchase_limit_info` | 없음 | `monitoring.limit_check` | 실사이트(상태 조회) · 해제 감지는 모의 |

## 4. 상세 창 (`DetailWindow` 4540)

| ID | GUI 요소 | 웹(이전) | 웹(현재) | 검증 |
|---|---|---|---|---|
| T1 | 상품 이미지 | 없음 | 상세 패널 이미지 | 화면 확인 |
| T2 | 브랜드·상품번호·품번·링크·카테고리·판매가·쿠폰할인·최종가·멤버할인%·적립률%·멤버 할인 가능·적립 선할인·goodsSavePoint·가능한 사이즈·선택 옵션코드 | 없음 | 상세 패널(사이즈별 값 포함) | 실사이트 |
| T3 | 크림 전체 거래건수 / 한달 거래수 | 없음 | 상세 패널 | 실사이트 |

## 5. GUI에 노출되지 않아 이식 대상에서 제외한 코드

| 코드 | 사유 |
|---|---|
| `ProxyManagerWindow`(2994), `open_proxy_manager`(2990) | `build_widgets`에 이 창을 여는 버튼이 없다. 호출 경로가 없는 코드다. |
| `on_click_start_auto_crawl`(1763), `start_brand_auto_crawl`(1293) | 존재하지 않는 `start_auto_crawl_button`을 참조한다. 버튼이 없고 실행하면 `AttributeError`가 난다. 같은 목적은 이벤트 자동화(A2~A5)로 제공된다. |
| `on_click_poizon_login`(974) 수동 POIZON 로그인 | 버튼에 연결되어 있지 않다. |
| `MonitoringWindow` 클래스 본문의 중복 메서드 | 파일 끝에서 `_patched_*` 함수로 덮어쓴다. 웹은 덮어쓴 쪽(실제 동작)을 기준으로 이식했다. |

## 6. 웹에서 동작이 달라진 점

| 항목 | GUI | 웹 | 이유 |
|---|---|---|---|
| 무신사 가격 | 상품당 첫 번째 구매 가능 옵션 1개의 회원가를 모든 사이즈 행에 표시 | 옵션마다 장바구니·주문서를 조회해 사이즈별 회원가 저장 | 옵션별 가격 차이를 반영하고 첫 사이즈 가격 복사를 막기 위함 |
| 목록 갱신 | 수집 시작 시 목록과 `products` 테이블을 비우고 다시 채움 | 기존 행을 유지하고 수집된 상품만 덮어씀. 제외는 숨김 처리 | 한쪽 실패로 다른 쪽 결과가 사라지지 않게 하기 위함 |
| 차액 부호 | 무신사 − KREAM | KREAM − 무신사(양수면 KREAM이 더 비쌈). CSV(E2)는 GUI 부호 유지 | 이전 웹 구현과 엑셀 열 정의를 유지 |
| 사이즈 매칭 | 숫자만 추출해 비교(US 8과 UK 8이 같게 처리될 수 있음) | 같은 체계끼리만 매칭, 근거 없는 환산 금지 | 잘못된 사이즈 매칭 방지 |
| KREAM 로그인 | `/login` 페이지 이메일 입력 | `/login/email` 페이지 | KREAM 로그인 화면 구조 변경(2026-10-09 확인) |
| KREAM 매칭·가격의 로그인 | 로그인 필수 | 로그인 없이 실행(거래량 필터만 로그인 필요) | 검색·시세 API가 공개 API임을 확인. 만료된 저장 토큰을 보내지 않음 |
| 입찰 엑셀 가격 | 부동소수점 계산(90,000×1.1 → 100,000) | Decimal 계산(99,000) | 반올림 오류 수정 |
| .env 계정 | 없음 | `MUSINSA_ID` 등으로 [.env 계정으로 로그인] | 사용자 요청 |
| 브라우저 | 매번 새 Chrome + 쿠키 파일 | 쇼핑몰별 전용 Chrome 프로필 | 세션 만료·쿠키 주입 누락 문제 감소 |

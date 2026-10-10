# 테스트 계획과 결과

테스트는 두 종류다. 섞어 읽지 않도록 문서도 나눴다.

| 종류 | 대상 | 위치 | 쇼핑몰 접속 |
|---|---|---|---|
| 모의 데이터 테스트 | 계산 규칙, 실패 처리, 작업 제어, 저장·복구, 내보내기, 마스킹 | `tests/` (이 문서) | 없음 |
| 실제 사이트 검증 | 로그인, 수집, 비교, 내보내기를 실제 쇼핑몰로 실행 | [live-verification.md](live-verification.md) | 있음 |

## 1. 실행

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

2026-10-10 결과: **130개 통과, 실패 0** (약 10~25초). 시작 시점의 기존 테스트 29개도 다시 실행해 통과를 확인한 뒤, 모듈 구조 변경에 맞춰 옮기고 54개를 추가했다.

| 파일 | 수 | 내용 |
|---|---|---|
| `test_crawler_pricing.py` | 2 | 기존. 회원가 계산식 |
| `test_database_cache.py` | 1 | 기존. 매칭 캐시 메타데이터 |
| `test_product_matcher.py` | 11 | 기존. 상품 매칭 검증기 |
| `test_web_workflow.py` | 15 | 이전 웹 테스트를 새 구조로 이전. 옵션별 가격, 403≠품절, 브라우저 어댑터, CSRF |
| `test_pipeline.py` | 25 | 신규. 수집 단계, KREAM 파서, 재시도·중단 규칙, 알림 기준 |
| `test_matching_eval.py` | 9 | 신규. 기존 매칭 규칙 재현, 평가 집계, 로컬 VLM 백엔드, 엑셀 가격 강조 규칙 |
| `test_vlm_benchmark.py` | 8 | 신규. 벤치마크 쌍 선정, 채점, 원격 모델 응답 처리, 연속 실패 시 중단 |
| `test_vlm_review.py` | 19 | 신규. VLM 제안 검증, 승인·거절·보류, 유료 호출 한도, 중단 후 재실행, KREAM 가격 수집 시간 초과 복구(테스트 장애 주입), 모델 판단을 제안으로만 저장하는 정책(`MatcherPolicyTests`) |
| `test_method_eval.py` | 7 | 신규. 방법 비교 사례 구성과 채점, 개발용·평가용 분리 |
| `test_web_app.py` | 33 | 신규. 로그인, 작업 제어, 복구, 내보내기, 모니터링, 로그 |

## 2. 격리

테스트가 사용자 데이터나 실제 쇼핑몰에 닿지 않도록 공통 준비 단계(`tests/helpers.py`)에서 막는다.

- DB는 테스트마다 임시 SQLite 파일을 쓴다.
- 검색 제외 CSV 경로를 임시 폴더로 바꾼다.
- `MUSINSA_ID` 등 계정 환경변수와 `KREAM_BEARER`를 지우고 `.env` 로더를 막는다.
- 실제 로그인 함수와 브라우저 생성 함수는 호출되면 실패한다.
- 로그는 실제 `logs/app.jsonl`에 쓰지 않는다.

이 장치들은 작업 중 실제로 사고가 나서 넣었다(트러블슈팅 T5, T15).

## 3. 요구사항별 회귀 테스트

### 로그인·계정정보

| 요구 | 테스트 |
|---|---|
| 쇼핑몰마다 자기 아이디·비밀번호 필요 | `test_each_shop_requires_its_own_account_and_password` |
| 입력한 계정이 그 쇼핑몰 로그인 함수에만 전달, 다른 쇼핑몰 상태 불변 | `test_credentials_reach_the_login_function_for_that_shop_only` |
| 한 쇼핑몰 로그인 실패가 다른 쇼핑몰 로그인을 깨지 않음 | `test_failed_login_of_one_shop_leaves_the_other_logged_in` |
| 비밀번호가 상태·로그·이력·DB에 남지 않음 | `test_password_never_appears_in_state_logs_or_database` |
| 쿠키만으로 성공 처리하지 않음, 서버 오류를 만료로 보지 않음 | `test_session_validity_needs_an_authenticated_answer` |
| 메모리 보관 계정은 디스크에 없고 삭제 가능 | `test_remembered_credentials_live_in_memory_only_and_can_be_dropped` |
| `.env` 계정 사용, 값 비노출 | `test_env_login_uses_env_values_without_exposing_them` |
| 수동 로그인 | `test_manual_login_needs_no_credentials` |
| 이전 프로세스의 "진행 중" 상태를 그대로 보여 주지 않음 | `test_stale_login_state_from_a_previous_process_is_not_shown_as_in_progress` |

### 독립 수집과 부분 실패

| 요구 | 테스트 |
|---|---|
| 무신사·KREAM 작업 동시 실행, 같은 쇼핑몰은 배타, 자기 작업만 중단 | `test_shops_run_independently_and_stop_only_their_own_job` |
| 중단 후 재실행 | `test_job_can_be_run_again_after_a_stop` |
| KREAM만 수집할 때 무신사를 호출하지 않고 저장값 재사용 | `test_kream_only_run_reuses_stored_musinsa_results` |
| 선행 조건 검사와 설명 | `test_prerequisites_are_explained`, `test_stage_without_prerequisite_fails_with_explanation`, `test_compare_requires_musinsa_results` |
| KREAM 실패가 무신사 결과와 이전 KREAM 값을 지우지 않음 | `test_kream_failure_keeps_previous_quotes_and_musinsa_data`, `test_kream_login_failure_does_not_touch_musinsa`, `test_kream_failure_in_full_run_keeps_musinsa_results` |
| 한 상품 수집이 다른 상품·매칭을 지우지 않음 | `test_collecting_one_product_does_not_remove_others_or_their_matches` |
| 인증 오류 시 즉시 중단, 저장분 유지 | `test_auth_error_stops_the_job_and_keeps_saved_sizes` |
| 후속 작업은 방금 수집한 상품만 대상 | `test_follow_up_jobs_default_to_the_last_collected_batch_only` |
| 주문 불가 상품은 한 번만 조회하고 묶음을 멈추지 않음 | `test_unorderable_product_is_asked_once_and_does_not_stop_the_batch` |
| 연속 실패 시 중단(무한 재시도 없음) | `test_repeated_failures_abort_instead_of_hammering`, `test_browser_denial_is_not_retried` |
| 재시작 후 복구 | `test_results_survive_a_server_restart`, `test_jobs_left_running_by_a_dead_server_are_marked_interrupted` |
| 숨김은 삭제가 아님 | `test_hiding_a_product_keeps_its_data_and_can_be_undone` |

### 가격·사이즈 정확성

| 요구 | 테스트 |
|---|---|
| 모든 사이즈 저장, 옵션별 가격 | `test_every_size_is_stored_and_each_option_gets_its_own_price`, `test_each_option_uses_own_order_price_and_coupon` |
| 첫 사이즈 가격 복사 금지 | `test_no_first_size_price_is_copied`, `test_grouped_mode_reuses_only_equal_option_prices_and_says_so` |
| 다른 옵션 주문서로 대체 금지 | `test_wrong_size_order_item_is_rejected` |
| 실패 ≠ 품절, 공개가 ≠ 회원가 | `test_failed_lookup_is_an_error_not_sold_out_and_never_a_public_price`, `test_failed_member_price_never_uses_public_price`, `test_denied_options_are_not_sold_out` |
| 재조회 실패 시 이전 값 유지 표시 | `test_refresh_failure_keeps_last_verified_price_as_stale` |
| 즉시구매 최저가 선택, 95점 제외 | `test_lowest_new_condition_instant_price_across_delivery_types`(실제 응답 발췌), `test_kream_lowest_instant_price_not_first_or_bid` |
| 입찰가를 즉시구매가로 쓰지 않음 | `test_bid_only_size_has_no_instant_price`, `test_buy_action_without_instant_wording_is_a_bid` |
| 즉시구매·즉시판매 분리 | `test_sell_side_is_separate_from_buy_side` |
| 사이즈 체계 간 추측 금지 | `test_size_keys_never_cross_systems`, `test_unknown_size_system_not_guessed`, `test_ambiguous_kream_sizes_are_not_matched` |
| 매칭 신뢰도·직접 지정 표시, 미검증 매칭 미사용 | `test_auto_match_is_stored_with_confidence`, `test_manual_match_approve_and_clear`, `test_unverified_match_is_not_priced`, `test_matching_respects_blacklist_and_duplicate_ids` |

### 내보내기·알림·모니터링·로그

| 요구 | 테스트 |
|---|---|
| 엑셀·CSV에 모든 사이즈, 출처, 조회 시각 | `test_downloads_contain_every_size_with_source_and_time` |
| 수식 주입 방지 | `test_csv_export_neutralises_formulas`, `test_export_keeps_text_safe_and_money_numeric` |
| 입찰 엑셀 가격(사이즈별, 1,000원 올림) | `test_bid_sheet_uses_each_sizes_own_price_and_only_matched_sizes` |
| 웹훅 검증·마스킹 | `test_webhook_is_validated_and_masked` |
| 재입고·가격 하락·구매제한 해제 감지 | `test_restock_detection`, `test_price_drop_detection_uses_member_price`, `test_purchase_limit_release_detection` |
| 자동화 주기 규칙(최소 30초) | `test_auto_event_interval_rule` |
| 작업 ID·쇼핑몰·상품·단계·오류 종류 추적 | `test_failure_kind_and_job_id_are_traceable_in_logs` |
| 마스킹 패턴, 회전 정책 | `test_redaction_patterns`, `test_rotation_policy_is_configured` |
| 입력 검증, 로컬 전용 | `test_collection_settings_validation`, `test_validation_and_empty_export`, `test_host_and_csrf_protection` |

## 4. 모의 테스트로만 확인한 것

아래는 실제 사이트로 실행하지 않았다. 코드와 모의 테스트는 있다.

- KREAM 계정 자동 입력 로그인의 제출 단계(무신사는 사용자가 실행해 성공)
- 자동 재로그인(메모리 보관 계정)
- 수집 작업 도중 중단 후 재실행
- 브랜드·카테고리 방식 수집
- 가격변동 모니터링, 이벤트 자동화(낮·밤 모드)
- 디스코드 알림 발송
- 재입고 "감지" 순간(재고 조회 자체는 실사이트 확인)

테스트가 없고 실행도 하지 않은 것: POIZON 로그인과 가격 수집(계정 없음). 기존 `PoizonSellerCrawler`를 그대로 호출하도록 이식만 했다.

## 5. 테스트가 잡아낸 실제 결함

| 결함 | 잡은 테스트 | 기록 |
|---|---|---|
| 작업 오류 문구에 비밀번호가 남을 수 있음 | `test_password_never_appears_in_state_logs_or_database` | T6 |
| 입찰가 부동소수점 반올림(99,000 → 100,000) | `test_bid_sheet_uses_each_sizes_own_price_and_only_matched_sizes` | T7 |

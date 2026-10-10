# 시스템 구조와 데이터 흐름

## 1. 구성

```
브라우저 (http://127.0.0.1:8765, 이 PC 전용)
   │  JSON API + CSRF 토큰
   ▼
shop_comparator/
├─ web/         web_app.py  Flask 앱, 입력 검증, 다운로드
│               templates/, static/
├─ services/    pipeline.py            수집 단계(무신사 / 매칭 / KREAM 가격 / 비교 / 필터 / POIZON / 전체)
│               jobs.py                작업 관리(작업 ID, 상태, 진행률, 중단, 쇼핑몰별 배타 실행)
│               vlm_review.py          VLM 제안 저장·검증, 사람의 승인·거절·보류, 업무량 집계
│               comparison_service.py  사이즈 정규화와 비교 행 계산(네트워크 없음)
│               monitoring.py          재입고·가격변동·구매제한 모니터링, 이벤트 자동화
│               exports.py             비교 엑셀/CSV, GUI 형식 CSV, KREAM 입찰 엑셀
├─ matching/    match_review.py    후보·이미지·모델번호 수집, 모델번호 대조, 확인용 엑셀
│               vlm_match.py       이미지 판정(OpenAI / 로컬 Ollama)
│               evaluate.py        기존 규칙 대비 성능 평가(고정 데이터)
│               vlm_benchmark.py   VLM 모델 비교(이미지 쌍 구성, 실행, 채점)
│               method_eval.py     방법 비교(규칙·텍스트·VLM·결합, 1:N)
│               product_matcher.py 기존 자동 판별기(규칙 + OpenAI)
├─ shops/       browser_transport.py  쇼핑몰 Chrome 페이지 안에서 fetch()로 요청을 보내는 어댑터
│               site_login.py         쇼핑몰별 로그인, 인증 확인, 메모리 계정 보관
│               crawler.py            무신사 API 호출·회원가 계산
│               kream_crawler.py, kream_api.py  KREAM 검색, 사이즈별 시세 파싱
│               poizon_seller_crawler.py        POIZON 셀러 센터(Playwright)
├─ storage/     database.py  기존 스키마와 매칭 캐시
│               store.py     웹용 테이블(추가만 함)
├─ core/        app_logging.py(중앙 로그·마스킹), crawl_errors.py, config.py, utils.py, webhook.py
└─ desktop/     gui.py 등 기존 Tkinter 프로그램
```

`core`는 다른 패키지를 import하지 않고, `storage`는 `core`만 import한다. `web`은 진입점이라 다른 패키지에서 import하지 않는다.

기존 Tkinter GUI(`shop_comparator/desktop/gui.py`)는 내용을 그대로 두었고 `python main.py --desktop`으로 실행된다. 웹과 GUI는 같은 SQLite 파일(`musinsa_data.sqlite`)을 쓴다.

## 2. 실행 단위: 작업(Job)

모든 수집·로그인은 `jobs.JobManager`가 만든 작업으로 실행된다.

| 항목 | 내용 |
|---|---|
| 작업 ID | 10자리 16진수. 그 작업이 남기는 모든 로그에 붙는다. |
| 상태 | 실행 중 → 완료 / 일부 실패 / 실패 / 중단됨. 서버가 꺼져서 끝나지 못한 작업은 다음 시작 때 "서버 종료로 중단"으로 바뀐다. |
| 자원 | 작업이 쓰는 쇼핑몰 목록. 같은 쇼핑몰을 쓰는 작업은 동시에 하나만 실행된다. 무신사 작업과 KREAM 작업은 동시에 실행된다. |
| 중단 | 작업마다 중단 플래그가 있다. 진행 중인 요청 하나가 끝나면 멈춘다. 이미 저장된 결과는 남는다. |
| 요약 | 성공·실패 건수, 단계별 오류. `job_runs` 테이블에 남아 내보내기·설정 탭의 작업 이력에 보인다. |

| 버튼 | API | 사용하는 쇼핑몰 | 선행 조건 |
|---|---|---|---|
| 무신사만 수집 | `POST /api/jobs/musinsa` | 무신사 | 회원가를 조회하면 무신사 로그인 |
| KREAM 매칭 | `POST /api/jobs/match` | KREAM | 무신사 상품 목록, KREAM 로그인 |
| KREAM 가격 수집 | `POST /api/jobs/kream` | KREAM | 매칭된 상품, KREAM 로그인 |
| 가격 비교 | `POST /api/jobs/compare` | 없음 | 무신사 사이즈 수집 결과 |
| 전체 순차 실행 | `POST /api/jobs/full` | 무신사, KREAM | 두 쇼핑몰 로그인 |
| 선택 상품만 갱신 | 위 API에 `ids` 전달 | 해당 쇼핑몰 | 상품·비교 탭에서 선택 |
| 거래량 필터 | `POST /api/jobs/filter` | KREAM | 매칭된 상품 |
| POIZON 가격 수집 | `POST /api/jobs/poizon` | POIZON | 무신사 상품 목록, POIZON 계정 |
| 작업 중단 | `POST /api/stop` (`id`) | | |

선행 조건이 맞지 않으면 작업은 요청을 보내기 전에 실패하고 이유를 알려 준다(예: "무신사 상품 목록이 비어 있습니다. 먼저 [무신사만 수집]을 실행해 주세요.").

## 3. 데이터 흐름

```
[무신사만 수집]
  목록 조회(직접 입력 / 랭킹 / 카테고리 / 브랜드 / 현재 목록)
   → 상품 상세 → 옵션·재고 → (구매 가능한 옵션마다) 장바구니 → 주문서 → 쿠폰 → 회원가 계산
   → products(상품 1행) + musinsa_variants(사이즈 1행씩)

[KREAM 매칭]
  products → KREAM 검색 → product_matcher 검증 → kream_search_cache(신뢰도·방식·근거)
  수동 지정은 매칭 탭에서 바로 kream_search_cache에 기록

[KREAM 가격 수집]
  검증된 매칭 → options/display(buy) + options/display(sell) → kream_quotes(사이즈 1행씩)

[가격 비교]  ← 네트워크 없음
  musinsa_variants ⨝ kream_search_cache ⨝ kream_quotes (사이즈 키가 같은 것만)
   → 화면 표, 비교 엑셀/CSV, 입찰 엑셀
```

비교 결과는 저장하지 않고 요청할 때마다 위 테이블에서 계산한다. 그래서 KREAM 수집이 실패해도 무신사 쪽 값은 그대로 보이고, 반대도 같다.

## 4. 저장소

기존 GUI 테이블은 구조와 내용을 유지한다. 웹은 아래 테이블을 추가로 쓴다(`shop_comparator/storage/store.py`, 모두 `CREATE TABLE IF NOT EXISTS`).

| 테이블 | 용도 | 비고 |
|---|---|---|
| `products` (기존) | 상품 1행. GUI와 공유 | 웹은 `upsert`만 한다. GUI의 `save_products`처럼 전체 삭제 후 삽입하지 않는다. |
| `kream_search_cache` (기존) | 무신사↔KREAM 매칭, 검증 여부·신뢰도·근거 | 수동 지정은 `match_method='manual'` |
| `web_product_state` | 목록 표시 여부(숨김), 쇼핑몰별 마지막 수집 상태·오류·시각 | 제외는 숨김 처리이고 데이터는 남는다. |
| `musinsa_variants` | 사이즈별 재고, 판매가, 쿠폰, 회원가, 출처, 오류, 조회 시각 | 조회 실패 시 이전 검증값을 지우지 않고 `stale`로 표시 |
| `kream_quotes` | 사이즈별 즉시구매가, 배송 유형, 즉시판매가, 입찰 전용 여부, 후보 목록, 조회 시각 | 성공한 조회로만 교체 |
| `poizon_quotes` | POIZON 사이즈별 가격 | |
| `job_runs` | 작업 이력 | |
| `monitor_items` | 모니터링 대상과 마지막 상태 | 재시작 후 목록 복구 |
| `web_settings` | 디스코드 웹훅, 알림 마진 | 웹훅은 응답에서 끝 6자만 노출 |
| `site_sessions` | 쇼핑몰별 로그인 상태, 마스킹된 계정, 마지막 인증 확인 시각 | 비밀번호·쿠키·토큰은 저장하지 않는다. |

### 재시작 후 복구

서버를 다시 켜면 상품 목록, 사이즈별 가격, 매칭, KREAM 시세, 모니터링 대상, 설정, 작업 이력이 DB에서 그대로 올라온다. 로그인은 쇼핑몰별 Chrome 프로필(`browser_profiles/`)에 남아 있어 다시 입력하지 않아도 되며, 다음 작업을 시작할 때 실제 인증 요청으로 유효한지 다시 확인한다. 실행 중이던 모니터링·자동화 루프는 자동으로 다시 시작하지 않는다.

## 5. 보안 경계

- 서버는 `127.0.0.1`에만 바인딩한다. 요청의 원격 주소와 Host 헤더를 모두 확인한다(DNS 리바인딩 방지).
- 모든 POST는 페이지에 심은 CSRF 토큰이 필요하다.
- CSP는 `default-src 'self'`이고 인라인 스크립트를 쓰지 않는다.
- 쇼핑몰 응답에서 온 문자열은 화면에서 `textContent`로만 넣고, 엑셀·CSV에서는 수식으로 해석되지 않게 처리한다.
- 브라우저 어댑터는 해당 쇼핑몰 도메인 밖으로는 요청하지 않는다.
- 주문·결제 API는 호출하지 않는다. 회원가 조회에 쓰는 요청은 "바로구매" 장바구니 담기, 주문서 조회, 쿠폰 조회 세 가지뿐이다.

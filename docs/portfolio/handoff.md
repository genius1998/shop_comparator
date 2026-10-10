# 인계 문서: 검토와 공개 포트폴리오 반영용

작성일 2026-10-10. 대상 저장소 `genius1998/shop_comparator`, 브랜치 `main`.
이 문서는 이번 보완 작업에서 **실제로 한 것, 실제로 잰 것, 하지 않은 것**을 구분해 적는다. 공개 포트폴리오의 배포나 PDF는 건드리지 않았다.

참고: 요청서에 "AGENTS.md를 확인하라"고 되어 있었으나 이 저장소에는 `AGENTS.md`가 없다.

---

## 1) 실제 완료한 기능

| 기능 | 내용 | 확인 방법 |
|---|---|---|
| 웹의 VLM 검토 흐름 | 상품 선택 → 검토 실행 → 진행 표시 → 카드에서 이미지·판정·사유·충돌 확인 → 승인·거절·보류 → 승인된 매칭을 가격 비교에 사용 | 테스트 13개, 데모 DB에서 브라우저 자동화로 실행·캡처 |
| 제안과 결정의 분리 | AI 제안은 `vlm_suggestions`, 사람의 결정은 `match_reviews`. 제안은 매칭을 바꾸지 않는다 | `test_stored_verdicts_become_suggestions_and_bad_ones_are_not_offered`, `test_only_an_approval_makes_the_match_used_for_prices` |
| 응답 검증 | 후보 밖 ID, 형식 오류, 오래된 후보에 대한 판정, 이미지 누락, API 실패를 제안으로 쓰지 않고 기록 | 같은 테스트 파일의 `SuggestionTests` |
| 유료 호출 제한 | 실시간 호출은 `max_calls`가 있어야 시작, 한도에서 정지. 기본은 저장된 판정 재생(네트워크 미호출) | `test_live_run_needs_a_call_limit_and_stops_at_it` |
| 중복 실행 방지, 중단 | 같은 후보 목록의 유효한 판정이 있으면 건너뜀. 중단하면 남은 상품은 미처리로 남음 | `test_running_again_does_not_duplicate...`, `test_cancelled_review_can_be_finished_later...` |
| 검수 이력과 시간 기록 | 결정마다 이전 매칭, 당시 제안, 검토 시간, AI 보조/수동 구분 저장. CSV 내보내기 | `test_metrics_count_only_what_people_actually_did`, `/api/export/review-log.csv` |
| 실패·미처리만 재실행 | 작업의 대상별 상태(`job_items`), 작업 이력의 재실행 버튼, `/api/jobs/<id>/resume` | `PriceRecoveryTests`(시간 초과 주입 후 누락·중복·손실 확인) |
| 방법 비교 평가 | A0·A·B·C·D를 1:N 선택으로 비교, "동일 상품 없음" 사례, 개발용·평가용 분리, 분자·분모 출력 | `python -m shop_comparator.matching.method_eval`, 테스트 7개 |
| 사이즈별 가격 엑셀 | 양쪽에 있는 사이즈를 한 행씩, 이미지, 강조 표시. 웹 버튼 | `test_price_check_download_has_one_row_per_shared_size_and_highlights` |
| 클릭 가능한 설명 페이지 | `docs/portfolio/index.html`. 13개 상세, 키보드 조작, 확대, 기능별 링크, 모바일 | `assets/page-check.json` |
| 캡처 재현 스크립트 | 데모 DB 생성, 재생 어댑터가 붙은 데모 서버, Playwright 캡처, 증빙 목록 생성 | `scripts/portfolio/` |

## 2) 핵심 변경 파일과 관련 함수

| 파일 | 함수·내용 |
|---|---|
| `shop_comparator/services/vlm_review.py` (신규) | `review_products`(작업 본문), `validate`(응답 검증), `candidate_key`, `image_problem`, `queue`(검토 화면 데이터), `_conflicts`, `decide`(승인·거절·보류), `metrics` |
| `shop_comparator/storage/store.py` | 테이블 `vlm_suggestions`, `match_reviews`, `job_items`. `add_suggestion`, `latest_suggestions`, `add_review`, `reviews`, `plan_items`, `mark_item`, `job_item_summary`, `job_run` |
| `shop_comparator/services/pipeline.py` | `collect_musinsa`·`quote_products`에 대상별 상태 기록, `unfinished`, `resume_musinsa`, `keep_batch` |
| `shop_comparator/web/web_app.py` | `/api/jobs/vlm`, `/api/review`, `/api/review/image/...`, `/api/review/<goodsNo>`, `/api/export/review-log.csv`, `/api/jobs/<id>/items`, `/api/jobs/<id>/resume`, `/api/export/price-check.xlsx`, 데모 표시 띠 |
| `shop_comparator/web/static/app.js`, `templates/index.html`, `static/app.css` | VLM 검토 탭(`loadReview`, `reviewCard`, `reviewDecide`), 작업 이력의 재실행 버튼 |
| `shop_comparator/services/comparison_service.py` | `match_info`: `human_review` → "검수 승인" |
| `shop_comparator/matching/method_eval.py` (신규) | `cases`, `method_a0`~`method_d`, `outcome`, `score`, `split_of`, `timing`, `build` |
| `shop_comparator/matching/match_review.py` | `export_xlsx`(사이즈별 시트), `price_level`, `price_summary`, `fill_match_images` |
| `scripts/portfolio/` (신규) | `build_demo.py`, `demo_server.py`, `capture.py`, `make_manifest.py` |
| `tests/test_vlm_review.py`, `tests/test_method_eval.py` (신규) | 20개 테스트 |
| `docs/method-evaluation.md`, `docs/review-measurement.md` (신규) | 새 실험 기록, 측정 절차 |

## 3) 테스트 명령과 실제 결과

2026-10-10에 `D:\shop_comparator`에서 실행한 결과다.

| 명령 | 결과 |
|---|---|
| `.\.venv\Scripts\python.exe -m unittest discover -s tests` | **130개 통과, 실패 0**. 쇼핑몰·유료 API 접속 없음 (이 문서를 처음 쓸 때는 124개) |
| `python -m shop_comparator.matching.evaluate` | 118 (48.0%) / 153 (62.2%) / 167 (67.9%). 이전과 같은 수치 |
| `python -m shop_comparator.matching.vlm_benchmark` | Qwen 92.0% / mini 87.0% / nano 73.0%. 이전과 같은 수치 |
| `python -m shop_comparator.matching.method_eval` | 아래 4)의 표 |
| `python scripts/portfolio/capture.py app` | 데모 서버에서 20개 화면 캡처, 오류 없이 종료 |
| `python scripts/portfolio/capture.py page` | 13개 상자 모두 패널 열림, 키보드로 열림, 이미지 확대, `#storage` 링크, 스크립트 오류 0, 모바일 가로 스크롤 없음, 이미지를 차단해도 본문 읽힘 |
| GitHub Actions | 이 문서를 쓴 시점의 마지막 확인은 이전 커밋 기준 통과. 이번 푸시의 결과는 저장소의 Actions 탭에서 확인해야 한다 |

실제 사이트로 한 검증은 이번 작업에 없다. 이번에 추가한 기능은 모의 테스트와 데모 DB(저장된 실데이터 재생)로만 확인했다.

## 4) 새로 측정한 지표

**표본**: 2026-10-09에 수집한 무신사 신발 랭킹 상품 중 KREAM 검색 후보가 있는 246개. 여기서 채점 가능한 사례 267개(정답 있음 167, 동일 상품 없음 94, 특정 후보가 아님 6).
**조건**: 저장된 결과만 사용, 네트워크 호출 없음. C는 `gpt-5.4-mini`의 저장 판정(이미지 + 상품명·모델번호 제공).
**계산**: 정밀도 = 맞은 동일 판정 ÷ 정오를 아는 동일 판정. 재현율 = 맞은 동일 판정 ÷ 정답이 있는 평가 대상. 사람에게 넘김 = 넘긴 수 ÷ 평가 대상.

평가용 154개 사례(개발용 113개 제외)

| 방법 | 정밀도 | 재현율 | 오매칭 | 사람에게 넘김 | 미실행 |
|---|---|---|---|---|---|
| A0 기존 규칙 | 61/152 (40.1%) | 61/83 (73.5%) | 91 | 0/154 | 0 |
| A 모델번호 규칙 | 77/77 (정의상) | 77/83 (92.8%) | 0 | 77/154 (50.0%) | 0 |
| B 텍스트 점수 | 12/13 (92.3%) | 12/83 (14.5%) | 1 | 82/154 (53.2%) | 0 |
| C VLM | 81/85 (95.3%) | 81/83 (97.6%) | 4 | 0/86 | 68 |
| D 규칙 + VLM | 83/86 (96.5%) | 83/83 (선택 편향) | 3 | 0/86 | 68 |

전체 267개 사례: A0 125/263 (47.5%), 오매칭 138 · A 153/153, 넘김 114/267 · B 32/33, 재현율 32/167 · C 162/171 (94.7%), 오매칭 9 · D 167/173 (96.5%), 오매칭 6.

"동일 상품 없음" 94개 사례: A0은 94개 전부에서 후보를 골랐다. A는 0개. B는 93개 구성 사례 중 78개를 "없음"으로 답했고 1개를 잘못 골랐다. C·D는 미실행.

VLM 호출 기록 246건(`gpt-5.4-mini-2026-03-17`): 응답 시간 중앙값 2.75초, p95 4.0초, 입력 504,773·출력 43,949 토큰, 약 0.58달러, 상품당 약 3.3원(공개 요금과 1달러 1,400원 적용).

**사람의 검토 시간**: 측정값 없음.

## 5) 기존 결과와 새 결과의 차이 및 한계

| 항목 | 기존 문서 | 이번에 바로잡거나 추가한 것 |
|---|---|---|
| 14개의 확인 주체 | "사람이 확인" | **틀린 표현이었다.** AI 보조 도구(Claude)가 이미지·상품명·모델번호로 판정했고 사람은 검수하지 않았다. 문서를 "AI 보조 검토(사람 미검수)"로 고쳤다. 수치는 그대로다 |
| 118 → 153 → 167 | "같은 상품을 찾은 수" | 확인해 확보한 매칭 수이며 정확도가 아니라고 명시. 증가 49개 = 규칙 개선 35 + VLM 제안 채택 14 |
| 평가 형태 | 방법별 확보 수, 1:1 이미지 쌍 100개 | 1:N 선택과 "동일 상품 없음"을 추가. 다섯 방법을 같은 사례로 비교 |
| 개발용·평가용 | 구분 없음 | 첫 100개와 그 변형 상품을 개발용으로 분리. 사후에 정한 기준이다 |
| 상품당 VLM 비용 | 약 2.7원 (10개 표본) | 약 3.3원 (246건 전체의 실제 토큰). 표본이 달라 생긴 차이다 |

한계

- **독립된 사람이 검수한 라벨이 없다.** 정답은 모델번호 일치(153)와 AI 보조 검토(21)에서 왔다.
- A의 정답은 A와 같은 근거로 정했다. A의 100%는 정확도 증거가 아니다.
- AI 보조 라벨은 VLM이 "동일"이라고 한 23개만 검토해 만들었다. VLM이 "다름"이라고 한 66개는 보지 않았다. C·D의 재현율은 부풀려져 있고 놓친 동일 상품 수는 모른다.
- 100쌍 벤치마크(1:1, 이미지만)와 이 평가(1:N, 이미지 + 상품 정보)는 조건이 다르다. 세 모델의 프롬프트와 실행 환경도 같지 않았다.
- 표본이 작다. 평가용에서 C의 오매칭 4건, D의 3건은 1~2건 차이로 순위가 바뀐다. 100쌍에서 nano의 오판 0건은 무오류를 뜻하지 않는다.
- 신발 한 범주, 한 시점의 데이터다.

## 6) 다이어그램·상세 패널·캡처 파일 위치와 실행 방법

| 항목 | 위치 |
|---|---|
| 설명 페이지 | `docs/portfolio/index.html` (파일을 바로 연다. 서버 불필요) |
| 스타일·동작 | `docs/portfolio/portfolio.css`, `portfolio.js` |
| 캡처 이미지 | `docs/portfolio/assets/*.png` (앱 화면 19장 + 모바일 1장 + 설명 페이지 5장) |
| 증빙 목록 | `docs/portfolio/evidence-manifest.json` |
| 재현 방법 | `docs/portfolio/README.md` |
| 스크립트 | `scripts/portfolio/build_demo.py` → `capture.py app` → `capture.py page` → `make_manifest.py` |

상세 패널의 주소: `index.html#web`, `#jobs`, `#collect`, `#normalize`, `#storage`, `#rules`, `#vlm`, `#review`, `#compare`, `#export`, `#monitor`, `#eval`, `#planned`.

캡처의 구분(각 이미지 설명과 `evidence-manifest.json`에 표시)

| 구분 | 뜻 | 해당 이미지 |
|---|---|---|
| 저장된 실데이터 재생·네트워크 미호출 | 2026-10-09 수집분의 사본을 그대로 표시 | 01, 01b, 02, 02a, 02c, 03, 04, 05, 06, 12, 13, 14, 15, 16 |
| 캡처 스크립트가 누른 동작 | 승인·거절 뒤의 화면. 사람의 검수가 아님 | 07, 07a, 08 |
| 테스트 장애 주입 | 가격 조회 시간 초과를 주입한 뒤의 화면 | 09, 10, 11 |

## 7) 공개 포트폴리오에 사용할 수 있는 검증된 설명 문장

아래 문장은 저장소의 코드, 테스트, 고정 데이터로 뒷받침된다. 괄호는 근거다.

1. "무신사 회원가와 KREAM 사이즈별 시세를 수집해 같은 사이즈끼리 비교하는 로컬 웹 애플리케이션을 설계하고 구현했습니다. 수집, 상품 매칭, 저장, 가격 비교, 웹 화면, 엑셀 내보내기를 포함합니다." (저장소 전체, 테스트 124개)
2. "실제 사이트에서 신발 랭킹 200개의 사이즈·재고·회원가를 수집했고, 매칭된 130개의 KREAM 사이즈별 시세를 수집해 114개 상품 1,155개 사이즈를 비교했습니다." (`docs/live-verification.md`, 2026-10-09)
3. "두 쇼핑몰이 프로그램의 직접 요청을 거절하는 원인을 요청 방식별 비교로 확인하고, 쇼핑몰 페이지 안에서 API를 호출하는 구조로 바꿨습니다." (`docs/collection-strategy.md`)
4. "기존 매칭 규칙은 품번이 맞지 않으면 첫 번째 검색 결과를 썼습니다. 246개 상품으로 재현해 보니 128개가 그렇게 처리됐고, 확인한 52개 중 44개가 다른 상품이었습니다." (`matching.evaluate`)
5. "모델번호 규칙을 확장해 확인된 매칭을 118개에서 153개로 늘렸고, 확정하지 못한 상품은 매칭하지 않게 했습니다." (`matching.evaluate`)
6. "규칙으로 확정하지 못한 상품에는 VLM이 후보를 제안하고, 사람이 웹 검수 화면에서 승인해야 가격 비교에 반영되도록 만들었습니다. AI의 제안과 사람의 결정은 별도 테이블에 이력으로 남습니다." (`services/vlm_review.py`, 테스트 13개)
7. "VLM의 응답은 형식, 후보 목록, 판정 시점을 검증하고, 모델이 출력한 신뢰도로는 자동 승인하지 않습니다. 틀린 '동일' 판정의 신뢰도가 0.84~0.98이었기 때문입니다." (`validate`, `docs/matching-evaluation.md`)
8. "같은 이미지 쌍 100개로 로컬 Qwen3-VL-8B, gpt-5.4-mini, gpt-5.4-nano를 비교했습니다. 정확도는 92%, 87%, 73%였고 다른 상품을 동일로 판정한 수는 5건, 2건, 0건이었습니다. 100쌍 규모의 결과입니다." (`matching.vlm_benchmark`)
9. "수집 작업이 중간에 실패해도 끝난 결과는 남고, 실패하거나 처리되지 않은 상품만 다시 실행할 수 있습니다. 시간 초과를 주입한 테스트로 누락과 중복이 없는지 확인했습니다." (`PriceRecoveryTests`)
10. "평가는 고정 데이터와 저장된 판정으로 다시 계산할 수 있고, 지표마다 분자와 분모를 함께 출력합니다." (`matching.method_eval --json`)

**쓰면 안 되는 표현**

- "사람은 택배만 보내면 된다", "완전 자동", "자동으로 수익을 낸다": 구현하지 않았다. 검수는 사람이 하고, 구매·입찰·결제는 프로그램이 하지 않는다.
- "정확도 67.9%" 또는 "VLM으로 49개를 더 찾았다": 67.9%는 확보율이고, VLM 제안에서 온 것은 14개다.
- "사람이 검수한 정답": 지금의 라벨은 AI 보조 검토다.
- "오판 0", "무오류": 소규모 표본의 결과다.
- "검토 시간 N% 단축": 측정하지 않았다.
- "순이익", "차익 실현": 화면의 값은 수수료·배송비를 뺀 값이 아니다. 2026-10-09 실행에서 KREAM 즉시판매가가 무신사 회원가보다 1만원 이상 높은 사이즈는 0개였다.

**작성자 확인이 필요한 문장**

- "직접 개발한 프로그램을 고객에게 판매했다": 이 저장소에는 판매 증빙이 없다. 설명 페이지에는 "작성자 설명"으로 표기했다. 공개 포트폴리오에 쓸 때는 작성자가 사실관계(시기, 대상, 형태)를 직접 적어야 한다.

## 8) 미완료 기능, 미실행 실험, 실제 사람 측정이 필요한 부분

| 구분 | 항목 | 필요한 것 |
|---|---|---|
| 사람 측정 필요 | 검수 화면을 쓴 실제 검수 | 작성자가 VLM 검토 탭에서 승인·거절. 지금 기록 0건 |
| 사람 측정 필요 | 수동 대비 AI 보조 검토 시간 | `docs/review-measurement.md`의 절차로 직접 측정 |
| 사람 측정 필요 | 사람이 검수한 라벨로 재평가, 독립 검수 | 검수 기록을 라벨로 사용. 가능하면 두 번째 검수자 |
| 미실행 실험 | B-LLM (텍스트만 주는 언어 모델) | 유료 호출 약 246건과 한도. 텍스트 전용 실행 옵션도 아직 없다 |
| 미실행 실험 | "동일 상품 없음" 93개 사례의 VLM 판정 | 유료 호출 93건 |
| 미실행 실험 | VLM이 "다름"이라고 한 66개의 검토(재현율) | 사람의 검수 |
| 미실행 실험 | 로컬 Qwen의 1:N 판정, 로컬로 넓게 찾고 API로 거르는 2단 구성 | 원격 서버가 1:1 API만 제공 |
| 미검증 | 웹에서의 실시간 VLM 호출 | 모의 테스트만 함. 실제 호출은 한도를 정해 한 번 실행해 봐야 한다 |
| 미검증 | 실제 쇼핑몰 장애에서의 재실행 | 테스트 어댑터로만 검증 |
| 미검증 | 디스코드 발송, 이벤트 자동화, 가격변동 모니터링, 브랜드·카테고리 수집, POIZON | 이전부터 미검증 |
| 미구현 | 자동 승인, 구매·입찰 자동화, 순이익 계산, 배포·다중 사용자 | 계획 단계. 설명 페이지에 보라색 점선으로 구분 |
| 미구현 | 무신사 수집의 "중단 지점부터 자동 재개" | 사용자가 [실패·미처리만 다시 실행]을 눌러야 한다 |
| 해결됨 (2026-10-10 추가 작업) | 웹의 자동 판별기가 모델의 판단을 바로 등록하던 경로 | 아래 "추가: AI 직접 등록 경로 통합" 참고 |

### 공개 전 확인

- 캡처에는 무신사·KREAM의 상품 이미지와 상품명, 가격이 들어 있다. 공개 범위와 대상 사이트의 이용약관을 작성자가 확인해야 한다.
- 캡처와 공개 파일에 계정, API 키, 쿠키, 웹훅 주소는 없다(데모 DB에서 삭제, `.env` 미사용). 서버 주소가 들어 있던 원격 VLM 클라이언트는 저장소에 없다.
- `docs/portfolio/assets/`의 이미지는 약 3.5MB다.

---

## 추가 (2026-10-10): AI 직접 등록 경로를 검수 정책으로 통합

**문제**: 검수 화면을 만든 뒤에도 `pipeline.match_products`가 자동 판별기의 결과를 방식과 무관하게 등록했다. 호출 경로는 넷이다: [KREAM 매칭](`match_kream`), [KREAM 가격 수집]의 누락 매칭 검색(`collect_kream(match_missing=True)`), [전체 순차 실행](`full`), 이벤트 자동화(`monitoring`의 두 곳). 운영 DB에는 언어 모델이 등록한 매칭 44건과 AI 보조 이미지 검토 2건이 있었다.

**변경**

| 파일 | 내용 |
|---|---|
| `services/pipeline.py` `match_products` | 규칙(`store.RULE_METHODS` = `model_code`, `local`) 결과만 등록. 그 밖의 방식은 `vlm_review.suggest_from_matcher`로 제안만 저장하고 `suggested`로 센다. 검수를 기다리거나 거절된 상품은 다시 검색하지 않는다 |
| `services/vlm_review.py` | `suggest_from_matcher`(제안 저장, 후보·이미지 추가, 중복 방지), `queue`(기존 AI 등록을 검수 대기로 표시), `decide`(승인·거절이 행을 지우지 않음) |
| `services/comparison_service.py` `match_info` | `kind`: `rule` / `user` / `ai_unreviewed` / `rejected` / `unverified`. `ai_unreviewed`와 `rejected`는 `KREAM_TRUST_EXISTING_MATCHES=1`이어도 가격 비교에 쓰지 않는다 |
| `storage/store.py` | `kream_search_cache`에 `review_state`, `origin_method`, `origin_reason` 열 추가(ALTER, 기존 값 유지). `review_approve`, `review_reject`, `RULE_METHODS`, `USER_METHODS`. `counts()`의 매칭 수에서 미검수 제외 |
| `web/web_app.py` | 매칭 탭의 [선택 승인]이 `vlm_review.decide`를 거쳐 검수 이력을 남김 |
| `web/static/app.js`, `templates/index.html` | 상태 표시와 필터, 검수 카드의 "AI 등록(미검수)" 표시 |
| `tests/test_vlm_review.py` `MatcherPolicyTests` | 6개 |

**기존 데이터**: 삭제한 행은 없다. 기존 AI 등록 행은 그대로이고 읽을 때 "AI 등록(미검수)"로 분류된다. 승인하면 `match_method='human_review'`, `origin_method`에 원래 방식이 남는다. 거절하면 `match_verified=0`, `review_state='rejected'`로 남는다.

**회귀 테스트** (`python -m unittest discover -s tests` → 130개 통과)

| 확인한 것 | 테스트 |
|---|---|
| 승인 전 가격 비교 제외, 승인 후 반영 | `test_llm_pick_is_a_suggestion_until_a_person_approves_it` |
| 거절 후 제외, 수집한 시세는 유지 | `test_rejected_suggestion_stays_out_of_the_price_comparison` |
| 네 작업 경로 모두 등록하지 않음, 승인 전 가격 요청 없음 | `test_every_job_path_goes_through_the_same_policy` |
| 규칙 확정과 사용자 선택의 구분 | `test_rule_and_user_matches_are_registered_and_told_apart` |
| 기존 AI 등록 보존, 미검수 표시, 출처 유지, 재검색 안 함 | `test_matches_registered_by_a_model_earlier_are_kept_but_wait_for_review` |
| 매칭 탭 승인도 검수 이력으로 | `test_approval_from_the_match_tab_is_recorded_as_a_review` |

**남은 한계**

- **실제 사람 검수와 검토 시간 측정은 여전히 없다.** 기존 AI 등록 46건도 아무도 검수하지 않았다. 알려진 오매칭(무신사 6931948 ↔ KREAM 973781)은 이제 가격 비교에서 빠져 있지만 "거절"로 처리된 것은 아니다.
- 미검수가 된 만큼 가격 비교 대상이 줄었다. 승인해야 다시 포함된다.
- 규칙 판별(`local`)은 바로 등록한다. 이 규칙 자체의 오판율은 측정하지 않았다.
- 이번 변경은 모의 테스트로만 확인했다. 실제 쇼핑몰 검색과 실제 모델 호출로 이 경로를 실행하지 않았다.
- 이벤트 자동화 경로는 `match_products`를 공유하므로 같은 정책이 적용되지만, 자동화 자체의 장시간 동작은 이전부터 미검증이다.
- 데스크톱 GUI(`desktop/gui.py`)는 자체 등록 경로를 그대로 쓴다. 웹과 같은 DB를 쓰므로 GUI로 매칭하면 모델의 선택이 등록될 수 있다. 그 행도 웹에서는 "AI 등록(미검수)"로 분류된다.

---

## 추가 2 (2026-10-10): 검증 근거 준비 — 사람 정답, 통합 확인, 시간 측정

목표는 기능 추가가 아니라, 품질과 AI 도입 효과를 검증할 수 있는 근거를 만드는 준비다. 유료 API 호출, 주문·결제, 운영 DB 수정 없이 진행했다(운영 DB는 읽기 전용으로만 열었고, 통합 확인 전후의 SHA-256이 같다).

### 1) 변경 사항

| 파일 | 내용 |
|---|---|
| `shop_comparator/matching/gold.py` (신규) | 평가 사례 고정(`build`), 화면에 보낼 데이터(`public_case`), 라벨 저장(`add_label`), 사람/자동화 구분(`origin_now`), 검토 시간(`add_timing`), 상태(`status`). 저장 위치는 `evaluation_data/gold/` |
| `shop_comparator/matching/gold_eval.py` (신규) | 사람 정답 기준 평가(`evaluate`, `score`). 사람 정답이 없으면 "사람 검수 대기"만 출력 |
| `shop_comparator/web/web_app.py` | `/api/gold`, `/api/gold/<사례>`, `/api/gold/image/...`, `/api/gold/report`, `/api/export/gold-labels.csv`, `/api/export/review-timing.csv`. 운영 검토 결정에 시간 기록 추가 |
| `shop_comparator/web/static/app.js`, `templates/index.html`, `static/app.css` | **정답 검수** 탭, 평가 결과 표, 검토 시간 기록 표, 탭 이탈 측정. 검토 탭에서 상품번호 여러 개로 묶음 선택 |
| `scripts/verify_review_policy.py` (신규) | 검수 정책 로컬 통합 확인 |
| `scripts/match_counts.py` (신규) | 매칭 수의 집계 기준 |
| `scripts/measurement_plan.py` (신규) | 검토 시간 측정 계획 |
| `tests/test_gold.py` (신규) | 10개 |
| `docs/gold-evaluation.md`, `policy-integration-check.md`, `verification-status.md` (신규), `review-measurement.md` (절차 보강) | |
| `docs/evaluation/gold/cases.json`, `method_outputs.json`, `docs/evaluation/integration/*.json` | 고정한 사례 정의, 통합 확인 결과, 집계 결과 |

정답 검수 화면을 만들다가 고친 버그: 검수자 칸에 별칭을 적은 뒤 처음 누른 후보 선택이 사라졌다(입력 칸이 포커스를 잃으며 화면을 다시 그림). 선택과 타이머를 유지하도록 고쳤다.

### 2) 테스트와 로컬 통합 확인 결과

| 명령 | 결과 | 구분 |
|---|---|---|
| `python -m unittest discover -s tests` | 140개 통과 | 모의 테스트 |
| `python scripts/verify_review_policy.py --save` | 21개 항목 통과, 실패 0 | 로컬 통합 확인 (저장 결과 재생, 승인·거절은 자동화 조작) |
| `python -m shop_comparator.matching.gold_eval` | "사람 검수 대기", 실행 범위만 출력 (A0·A·B 60/60, C·D 53/60, B-LLM 미실행) | 사람 정답 0건 |
| `python scripts/portfolio/capture.py app` / `page` | 앱 화면 27장, 설명 페이지 5장. 14개 상자 모두 열림, 오류 0 | 저장 결과 재생 / 모의 장애 / 자동화 조작 |
| `evaluate`, `vlm_benchmark`, `method_eval` | 이전과 같은 수치 | 저장된 결과 |

매칭 수의 집계 기준(`docs/policy-integration-check.md` 2절): 전체 행 16,339, 상품 목록 371. AI 등록 46건은 전체 행 기준이고 그중 28건이 상품 목록의 상품이다. 화면의 183 → 155는 활성 상품 기준이며 차이 28이 그 미검수 상품이다. 가격 비교 대상은 179 → 154.

### 3) 캡처와 설명 페이지

`docs/portfolio/index.html` (상자 14개. **정답 검수 (평가용)** 추가). 새 캡처:

| 파일 | 내용 | 구분 |
|---|---|---|
| `17-before-approval.png` | 승인 전: "AI 등록(미검수)", KREAM 가격 비어 있음 | 저장 결과 재생 |
| `07-approved-in-comparison.png` | 승인 후: "검수 승인", 가격 반영 | 재생 + 자동화 조작 |
| `19-after-reject.png` | 거절 후: "검수 거절", 가격 비어 있음 | 재생 + 자동화 조작 |
| `18-legacy-ai-card.png`, `15-match.png` | 기존 AI 등록의 미검수 표시 | 저장 결과 재생 |
| `20-gold-blind.png` | AI 결론을 숨긴 정답 검수 화면 | 저장 결과 재생 |
| `21-gold-report-waiting.png` | 평가 결과: 실행·미실행 수와 "사람 검수 대기" | 저장 결과 재생 |
| `22-timing-log.png`, `23-gold-progress.png` | 검토 시간 기록, 자동화 기록은 사람 정답으로 세지 않음 | 자동화 조작 |

이 묶음에 쇼핑몰이나 모델을 실제로 호출한 화면은 없다. 승인·거절·정답 저장이 보이는 화면은 모두 자동화 조작이며 사람 검수의 증거가 아니다(`evidence-manifest.json`의 `category`, `humanReviewEvidence`).

### 4) 현재 실제 사람 검수 건수

| 항목 | 건수 |
|---|---|
| 사람이 만든 평가 정답 | **0 / 60** |
| 사람이 한 운영 검수(승인·거절·보류) | **0** |
| 사람의 검토 시간 측정 | **0** |

### 5) 정답 검수를 시작하는 방법

1. `http://127.0.0.1:8765/#gold` 를 연다 (서버가 꺼져 있으면 `powershell -ExecutionPolicy Bypass -File restart_server.ps1`).
2. **검수자 (별칭)** 에 별칭 입력.
3. 사례마다 같은 상품인 후보의 **[후보 N 선택]** → **[선택한 후보가 같은 상품 · 저장]**. 없으면 근거를 적고 **[동일 상품 없음 · 저장]**, 가릴 수 없으면 **[판단 불가 · 저장]**.
4. 60건까지 반복. 중간에 닫아도 진행은 남는다.

### 6) 검수를 마친 뒤 실행할 명령

```powershell
.\.venv\Scripts\python.exe -m shop_comparator.matching.gold status
.\.venv\Scripts\python.exe -m shop_comparator.matching.gold_eval --save
.\.venv\Scripts\python.exe -m shop_comparator.matching.gold_eval --json
.\.venv\Scripts\python.exe -m shop_comparator.matching.gold export
```

### 7) 검토 시간 측정 방법

`docs/review-measurement.md` 3절. 요약: `python scripts/measurement_plan.py --per-block 12 --session 1` → 출력된 블록 A 목록을 VLM 검토 탭 검색창에 붙여넣고 "AI 제안 숨기기"를 켠 채 검토 → 쉬었다가 블록 B를 끈 채 검토 → 정답 검수 탭의 [검토 시간 CSV]. 세션 2는 순서를 바꿔 새 상품으로.

### 8) 아직 입증되지 않은 주장과 남은 한계

- 검토 시간 감소: 측정 없음.
- 매칭 정확도, VLM 제안의 정확도, 규칙(`local`)의 정확도: 사람이 만든 정답이 없다.
- 통합 확인은 저장 결과 재생이다. 실제 쇼핑몰 검색과 실제 모델 호출에서의 동작은 확인하지 않았다.
- 60개 사례는 무작위 표본이 아니고 어려운 사례의 비중이 높다. 전체 정확도로 일반화할 수 없다.
- 검수자가 한 명이면 검수자 간 일치도를 알 수 없다.
- 정답 검수 화면에 모델번호가 보인다(요청 사항). 사람의 판단이 모델번호 쪽으로 기울 수 있다.
- 미실행: 텍스트 전용 LLM, "동일 상품 없음" 사례의 VLM 판정, 저장된 판정이 없는 7건의 VLM 판정.
- 상세한 단계별 상태는 `docs/verification-status.md`.

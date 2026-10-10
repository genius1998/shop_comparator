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
| `.\.venv\Scripts\python.exe -m unittest discover -s tests` | **124개 통과, 실패 0** (약 35초). 쇼핑몰·유료 API 접속 없음 |
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
| 알려진 문제 | 웹의 [KREAM 매칭]이 설정에 따라 `gpt-5.4-nano`의 판단을 바로 등록 | 색상이 다른 상품 1건(무신사 6931948)이 등록돼 있다. 검수 화면에서 충돌로 표시된다. 이 경로를 검수 대기열로 돌리는 수정이 남았다 |

### 공개 전 확인

- 캡처에는 무신사·KREAM의 상품 이미지와 상품명, 가격이 들어 있다. 공개 범위와 대상 사이트의 이용약관을 작성자가 확인해야 한다.
- 캡처와 공개 파일에 계정, API 키, 쿠키, 웹훅 주소는 없다(데모 DB에서 삭제, `.env` 미사용). 서버 주소가 들어 있던 원격 VLM 클라이언트는 저장소에 없다.
- `docs/portfolio/assets/`의 이미지는 약 3.5MB다.

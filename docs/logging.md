# 실행 로그와 진단 자료

## 1. 중앙 로그 (`shop_comparator/core/app_logging.py`)

웹 앱의 모든 모듈은 `app_logging.log()` 하나로 기록한다. 기존 모듈이 받는 `log_callback(text)`도 `app_logging.callback()`으로 같은 곳에 모인다.

레코드 필드

| 필드 | 내용 |
|---|---|
| `ts` | UTC 시각(밀리초) |
| `level` | `info` / `warning` / `error` |
| `job` | 작업 ID. 모니터링은 `monitor-restock` 등, 자동화는 `auto-event` |
| `shop` | `musinsa` / `kream` / `poizon` |
| `goods` | 무신사 상품번호 |
| `stage` | 단계: `login`, `session-check`, `musinsa`, `musinsa-price`, `kream-match`, `kream-price`, `kream-filter`, `compare`, `poizon`, `monitor-*`, `auto-event`, `notify`, `export`, `server` |
| `error` | 오류 종류: `auth`, `blocked`, `rate_limit`, `http`, `network`, `format`, `schema`, `api`, `pricing`, `timeout`, `busy`, `prerequisite`, `cancelled`, `input`, `internal` |
| `message` | 마스킹을 거친 문장 |
| `seq` | 서버 실행 중 일련번호 |

`job`, `shop`, `goods`, `stage`는 `with app_logging.bind(...)` 로 묶어 두면 그 안에서 남기는 모든 레코드에 자동으로 붙는다. 작업 관리자가 작업 ID를 묶고, 각 단계가 쇼핑몰과 상품번호를 묶는다.

## 2. 저장 위치와 보관

| 위치 | 내용 | 보관 |
|---|---|---|
| `logs/app.jsonl` | 구조화 로그(JSON Lines) | 5MB마다 회전, 백업 5개(`app.jsonl.1`~`.5`). 최대 약 30MB |
| 메모리 버퍼 | 최근 3,000건. 웹 로그 탭이 읽음 | 서버 종료 시 사라짐 |
| `logs/server.out.log`, `logs/server.err.log` | `restart_server.ps1`로 띄운 서버의 표준 출력·접근 로그 | 재시작 때 덮어씀 |
| `job_runs` 테이블 | 작업별 시작·종료 시각, 결과, 요약, 오류 종류 | DB에 계속 남음 |

`logs/` 폴더 전체가 `.gitignore` 대상이다. 원본 로그는 저장소에 올리지 않는다.

## 3. 웹에서 보기

로그 탭에서 작업, 쇼핑몰, 수준으로 걸러 보고 메시지나 상품번호로 검색한다. 상단 작업 표시줄의 [로그] 버튼은 그 작업 ID로 바로 걸러 준다.

API: `GET /api/logs?after=<seq>&job=<id>&shop=<shop>&level=<level>&q=<검색어>`

## 4. 민감정보 마스킹

레코드를 파일이나 버퍼에 넣기 전에 `redact()`를 거친다.

| 대상 | 처리 |
|---|---|
| 사용자가 입력한 비밀번호 | 로그인 요청을 받는 즉시 등록해 정확히 일치하는 문자열을 `***`로 치환 |
| `Bearer <토큰>`, JWT 형태 문자열 | `***` |
| `app_atk=`, `access_token:`, `password=`, `cookie:`, `authorization:` 등의 값 | `***` |
| 디스코드 웹훅 URL | `https://discord.com/api/webhooks/***` |
| 이메일 주소 | `ab***@domain` |

쇼핑몰 응답 본문, 쿠키, 요청 헤더는 로그에 넣지 않는다. 예기치 않은 예외는 예외 종류만 사용자에게 보여 준다. 작업 오류 문구도 같은 마스킹을 거쳐 저장한다.

기존 `shop_comparator/shops/crawler.py`의 요청·응답 덤프(`print`)는 기본으로 꺼져 있고 `SHOP_DEBUG_HTTP=1`일 때만 표준 출력으로 나온다. 이 출력에는 응답 본문이 들어가므로 공유하지 않는다.

## 5. 예시 (민감정보 제거)

2026-10-09 실제 실행에서 발췌했다. 상품번호는 공개 상품 페이지 번호다.

```json
{"ts":"2026-10-09T04:09:17.219+00:00","level":"info","job":"826244640d","shop":null,"goods":null,"stage":"musinsa","error":null,"message":"작업 시작: 무신사 수집","seq":2}
{"ts":"2026-10-09T04:09:37.992+00:00","level":"info","job":"826244640d","shop":"musinsa","goods":"1527076","stage":"musinsa","error":null,"message":"(1/1) 무신사 상품 조회","seq":3}
{"ts":"2026-10-09T04:09:42.341+00:00","level":"info","job":"826244640d","shop":null,"goods":null,"stage":"musinsa","error":null,"message":"작업 종료: 무신사 수집 · 완료 · {'targets': 1, 'ok': 1, 'goods': ['1527076']}","seq":4}
{"ts":"2026-10-09T04:13:16.117+00:00","level":"error","job":"003a1c5cd1","shop":null,"goods":null,"stage":"musinsa","error":"auth","message":"무신사: 무신사 로그인이 필요합니다. 로그인 탭에서 다시 로그인해 주세요. (주문서 API가 인증을 거절했습니다 (HTTP 401).)","seq":12}
```

마스킹 예(테스트 `test_password_never_appears_in_state_logs_or_database`의 입력과 저장 결과)

```
입력: debug: typed password=<입력한 비밀번호> for me@example.com
저장: debug: typed password=*** for me***@example.com
```

## 6. 문제를 추적하는 순서

1. 상단 작업 표시줄이나 작업 이력에서 실패한 작업의 오류 종류를 본다.
2. [로그]를 눌러 그 작업 ID의 레코드를 본다. `goods`와 `stage`로 어느 상품, 어느 단계에서 멈췄는지 확인한다.
3. 오류 종류별 조치

| 오류 종류 | 뜻 | 조치 |
|---|---|---|
| `auth` | 로그인 만료 또는 미로그인 | 로그인 탭에서 해당 쇼핑몰 재로그인 |
| `blocked` | 접근 거절(403) | 일반 브라우저로 해당 쇼핑몰 접속·인증 상태 확인 후 시간을 두고 재실행 |
| `rate_limit` | 요청 한도(429) | 시간을 두고 재실행 |
| `schema`, `format` | 응답 구조가 예상과 다름 | 쇼핑몰 변경 가능성. `SHOP_DEBUG_HTTP=1`로 응답 확인 |
| `pricing` | 장바구니·주문서에서 해당 옵션을 확인하지 못함 | 상품의 구매 제한·품절 여부 확인 |
| `prerequisite` | 선행 작업 없음 | 안내된 작업을 먼저 실행 |
| `busy` | 같은 쇼핑몰을 다른 작업이 사용 중 | 그 작업이 끝난 뒤 실행 |

4. 더 긴 기간은 `logs/app.jsonl`을 연다. PowerShell 예:

```powershell
Get-Content logs\app.jsonl | ConvertFrom-Json | Where-Object { $_.job -eq '826244640d' } | Format-Table ts, level, shop, goods, stage, error, message -AutoSize
```

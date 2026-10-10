# 포트폴리오 설명 페이지

`index.html`은 시스템 구조를 클릭 가능한 그림으로 보여 주고, 기능마다 문제와 역할, 입력 → 처리 → 출력, 내부 흐름, 실행 화면, 코드 예시, 소스 위치, 실패 처리와 한계를 엽니다.

## 열기

정적 파일입니다. 서버 없이 파일을 바로 열면 됩니다.

```powershell
start docs\portfolio\index.html
```

| 파일 | 내용 |
|---|---|
| `index.html` | 그림, 13개 상세(12개 기능 + 계획 단계 1개). 스크립트 없이도 모든 상세가 문서로 읽힙니다 |
| `portfolio.css`, `portfolio.js` | 스타일, 패널·확대·기능별 링크(`#vlm` 등)·키보드 조작 |
| `assets/*.png` | 실행 화면 캡처 |
| `evidence-manifest.json` | 이미지마다의 출처, 캡처 조건, 관련 기능, 구분(재생 / 테스트 장애 주입 / 자동화 / 페이지) |
| `assets/captures.json`, `assets/page-check.json` | 캡처 스크립트가 남긴 원본 기록과 페이지 동작 확인 결과 |
| `handoff.md` | 검토와 공개 포트폴리오 반영을 위한 인계 문서 |

외부 자원(폰트, CDN)을 쓰지 않고 상대 경로만 씁니다. 폴더째 옮기면 그대로 동작합니다.

## 조작

- 상자를 누르거나 Tab으로 이동해 Enter 또는 Space.
- Esc 또는 [닫기]로 패널을 닫습니다. 닫으면 원래 상자로 초점이 돌아갑니다.
- 이미지를 누르면 확대됩니다.
- 주소 끝에 `#vlm`, `#storage`, `#jobs`처럼 기능 이름을 붙이면 그 상세가 열린 채로 뜹니다. 패널의 [이 기능 링크 복사]로 주소를 복사합니다.
- 760px 이하에서는 그림 대신 기능 목록이 나옵니다.

## 캡처 다시 만들기

운영 DB는 읽기만 합니다. 캡처는 사본에서 찍습니다.

```powershell
# 1. 데모 폴더 생성: 운영 DB를 SQLite backup으로 복사하고 쿠키·세션·웹훅 설정을 지운다
.\.venv\Scripts\python.exe scripts\portfolio\build_demo.py

# 2. 앱 화면 캡처: 데모 서버(127.0.0.1:8799)를 띄우고 Playwright로 조작한다
.\.venv\Scripts\python.exe scripts\portfolio\capture.py app

# 3. 설명 페이지 캡처와 동작 확인(데스크톱, 모바일, 클릭, 키보드, 이미지 차단)
.\.venv\Scripts\python.exe scripts\portfolio\capture.py page

# 4. 증빙 목록 갱신
.\.venv\Scripts\python.exe scripts\portfolio\make_manifest.py
```

필요한 것: 설치된 Google Chrome, `playwright` 패키지(`requirements.txt`에 있음), 그리고 `match_review/`에 저장된 후보·이미지·VLM 판정(2026-10-09 수집분). `match_review/`와 `portfolio_demo/`는 git에 올리지 않으므로 다른 PC에서는 수집을 먼저 해야 캡처를 다시 만들 수 있습니다.

### 데모 서버가 하는 일 (`scripts/portfolio/demo_server.py`)

| 항목 | 동작 |
|---|---|
| 데이터 | `portfolio_demo/`의 DB 사본. 40개 상품을 "방금 수집한 상품"으로 지정 |
| 네트워크 | 쇼핑몰과 API를 호출하지 않는다. `.env`를 읽지 않아 계정과 키가 없다. 로그인 함수는 거절로 대체 |
| KREAM 가격 | 사본에 저장된 시세를 그대로 돌려준다(재생) |
| VLM | 2026-10-09에 저장된 `gpt-5.4-mini` 판정을 불러온다. 모델을 호출하지 않는다. 진행 표시가 보이도록 상품당 0.25초 지연 |
| 장애 | `--fail`로 지정한 상품의 가격 조회가 처음 두 번 시간 초과된다. **테스트 장애 주입**이다 |
| 표시 | 모든 화면 상단에 "데모 · 저장된 실데이터 재생 · 네트워크 미호출" 띠가 나온다 |

### 캡처에서 실제가 아닌 것

- 승인·거절은 캡처 스크립트가 눌렀습니다. 사람의 검수가 아닙니다.
- 검토 시간과 작업 소요 시간은 사람의 업무 시간이나 실제 수집 시간이 아닙니다.
- 실패와 재실행 화면의 실패는 주입한 것입니다.
- `12-export.png`는 실제로 내려받은 엑셀의 첫 시트를 표로 옮겨 그린 미리보기이며 엑셀 프로그램 화면이 아닙니다.
- 데모 DB에서 상품 2개(3976350, 4207634)의 기존 매칭을 해제해 검수 대기 상태로 되돌렸습니다. 그 매칭은 AI 보조 검토로 등록돼 있던 것입니다.

## 다른 사이트로 옮길 때

`docs/portfolio/` 폴더 전체를 복사하면 됩니다. 상세 본문은 `index.html`의 `<article class="detail" data-detail="...">` 안에 있어 기능 단위로 떼어 쓸 수 있습니다. 공개하기 전에 `handoff.md`의 "공개 전 확인" 항목을 봐 주세요.

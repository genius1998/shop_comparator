# 문서 안내

무신사 × KREAM 가격 비교 프로그램의 설계, 검증, 문제 해결 기록이다. 실행 방법은 [루트 README](../README.md)에 있다.

| 문서 | 내용 |
|---|---|
| [feature-map.md](feature-map.md) | 기존 Tkinter GUI의 화면·버튼·설정 전체와 연결 함수, 웹 구현 위치, 항목별 검증 상태. 이식하지 않은 코드와 동작이 달라진 점 |
| [architecture.md](architecture.md) | 모듈 구성, 작업(Job) 모델, 데이터 흐름, DB 테이블, 재시작 복구, 보안 경계 |
| [login-and-sessions.md](login-and-sessions.md) | 쇼핑몰별 로그인 흐름, 인증 확인 방법, 상태 표시, 만료·추가 인증 처리, 계정정보·쿠키·토큰 취급 |
| [pricing-and-matching.md](pricing-and-matching.md) | 무신사 회원가 계산식과 포함 범위, KREAM 즉시구매·즉시판매·입찰 구분과 최저가 선택 기준, 사이즈 정규화, 상품 매칭 상태 |
| [collection-strategy.md](collection-strategy.md) | 직접 HTTP·브라우저 내 API·화면 조작·HTML 파싱 중 무엇을 왜 골랐는지, 2026-10-09 재현 결과 |
| [matching-evaluation.md](matching-evaluation.md) | 기존 매칭 규칙의 문제, 모델번호 대조·VLM·사람 확인의 결과 비교, 로컬 Qwen3-VL-8B·gpt-5.4-mini·gpt-5.4-nano 벤치마크(정확도·비용·장단점), 평가 방법과 한계 |
| [troubleshooting.md](troubleshooting.md) | 실제로 발생한 문제 25건. 증상, 재현 조건, 관측 근거, 원인, 수정, 검증, 남은 한계 |
| [testing.md](testing.md) | 모의 데이터 테스트 104개의 구성과 요구사항별 대응, 격리 방법 |
| [live-verification.md](live-verification.md) | 실제 무신사·KREAM을 상대로 한 실행 기록(작업 ID, 가격, 비교표)과 미검증 항목 |
| [logging.md](logging.md) | 중앙 로그의 필드, 저장 위치와 회전 정책, 웹 로그 탭, 마스킹, 민감정보를 제거한 예시, 문제 추적 순서 |
| [limitations.md](limitations.md) | 검증하지 못한 것, 동작상의 제한, 쇼핑몰 변경에 취약한 지점, 정리 과제 |

화면 캡처는 `images/web-*.png`에 있다(로그인, 수집, 상품·비교, 매칭, 모니터링, 내보내기, 로그 탭).

## 읽는 순서

1. 무엇을 옮겼는지: [feature-map.md](feature-map.md)
2. 어떻게 만들었는지: [architecture.md](architecture.md) → [collection-strategy.md](collection-strategy.md) → [pricing-and-matching.md](pricing-and-matching.md)
3. 실제로 되는지: [live-verification.md](live-verification.md) → [testing.md](testing.md)
4. 무엇이 막혔고 어떻게 풀었는지: [troubleshooting.md](troubleshooting.md) → [matching-evaluation.md](matching-evaluation.md)
5. 무엇이 남았는지: [limitations.md](limitations.md)

## 모의 테스트와 실사이트 검증의 구분

- "모의"라고 쓴 것은 `tests/`의 단위·통합 테스트로만 확인한 것이다. 쇼핑몰에 접속하지 않는다.
- "실사이트"라고 쓴 것은 2026-10-09에 실제 쇼핑몰 응답으로 확인한 것이고, [live-verification.md](live-verification.md)에 작업 ID와 값이 있다.
- "미검증"이라고 쓴 것은 실행해 보지 않은 것이다.

## 민감정보

이 폴더의 문서와 이미지에는 비밀번호, 쿠키, 토큰, 웹훅 URL, 계정 식별자가 없다. 로그 예시는 실제 기록에서 공개 상품번호만 남긴 것이다. 원본 로그(`logs/`), 브라우저 프로필(`browser_profiles/`), DB, `.env`는 git에서 제외된다.

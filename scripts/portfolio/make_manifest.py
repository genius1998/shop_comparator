"""Write docs/portfolio/evidence-manifest.json from the capture records.

    python scripts/portfolio/make_manifest.py
"""
import hashlib
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PORTFOLIO = os.path.join(ROOT, "docs", "portfolio")
KINDS = {
    "replay": "저장된 실데이터 재생·네트워크 미호출",
    "fault-injection": "테스트 장애 주입 (저장된 실데이터 재생 위에서 시간 초과를 주입, 실제 쇼핑몰 미접속)",
    "automation": "저장된 실데이터 재생 + 캡처 스크립트가 누른 동작 (사람의 검수·측정이 아님)",
    "page": "설명 페이지 자체의 화면 (데이터 없음)",
}
# The four things a reader has to tell apart.  No image in this set is a real run against a shop or a model.
CATEGORY = {"replay": "저장 결과 재생", "fault-injection": "모의 장애", "automation": "자동화 조작", "page": "설명 페이지 화면"}
PAGE = [
    ("page-desktop", "설명 페이지 첫 화면 (1440px)", "portfolio-page"),
    ("page-panel-vlm", "VLM 판정 상세 패널을 연 화면", "portfolio-page"),
    ("page-panel-storage", "기능별 링크(#storage)로 바로 연 저장 상세 패널", "portfolio-page"),
    ("page-mobile", "설명 페이지 모바일 화면 (390px, 그림 대신 기능 목록)", "portfolio-page"),
    ("page-mobile-panel", "모바일에서 연 상세 패널", "portfolio-page"),
]


def digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def main():
    with open(os.path.join(PORTFOLIO, "assets", "captures.json"), encoding="utf-8") as f:
        record = json.load(f)
    try:
        with open(os.path.join(PORTFOLIO, "assets", "page-check.json"), encoding="utf-8") as f:
            check = json.load(f)
    except OSError:
        check = None
    images = []
    for item in record["captures"]:
        path = os.path.join(PORTFOLIO, item["file"])
        images.append({**item, "category": CATEGORY[item["kind"]], "kindLabel": KINDS[item["kind"]], "exists": os.path.exists(path),
                       "sha256_16": digest(path) if os.path.exists(path) else None})
    for name, title, feature in PAGE:
        path = os.path.join(PORTFOLIO, "assets", name + ".png")
        if os.path.exists(path):
            images.append({"file": f"assets/{name}.png", "title": title, "feature": feature, "dataSource": "docs/portfolio/index.html",
                           "kind": "page", "category": CATEGORY["page"], "kindLabel": KINDS["page"], "detail": "Playwright로 연 정적 페이지.", "exists": True,
                           "sha256_16": digest(path)})
    manifest = {
        "generatedBy": "scripts/portfolio/make_manifest.py",
        "captureTool": "Playwright (설치된 Google Chrome, 헤드리스), 1440x1000 @1.25배 / 모바일 390x844 @2배",
        "app": "scripts/portfolio/demo_server.py 로 띄운 데모 서버 (127.0.0.1:8799)",
        "demoDatabase": record["demo"],
        "faultInjection": record["faultInjection"],
        "kinds": KINDS,
        "categories": {"실제 실행": "이 묶음에는 없음. 쇼핑몰과 모델을 실제로 호출한 화면은 캡처하지 않았다.", **{v: KINDS[k] for k, v in CATEGORY.items()}},
        "humanReviewEvidence": "없음. 승인·거절·정답 저장이 보이는 화면은 모두 자동화 조작이며 사람 검수의 증거가 아니다.",
        "notRealOperation": ["승인·거절은 캡처 스크립트가 누른 것이며 사람의 검수가 아니다.",
                             "검토 시간, 작업 소요 시간은 사람의 업무 시간이나 실제 수집 시간이 아니다.",
                             "VLM 판정은 2026-10-09에 저장된 응답을 불러온 것이며 캡처 때 모델을 호출하지 않았다.",
                             "진행 표시 캡처를 위해 데모 서버가 상품당 0.25초 지연을 넣었다."],
        "secretsCheck": "데모 DB에서 cookies, site_sessions, webhook 설정을 삭제했고 .env를 읽지 않는다. 캡처에 계정, 키, 쿠키, 웹훅 주소가 없다.",
        "pageCheck": check,
        "images": images,
    }
    with open(os.path.join(PORTFOLIO, "evidence-manifest.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print("images:", len(images), "| missing:", [i["file"] for i in images if not i["exists"]])


if __name__ == "__main__":
    main()

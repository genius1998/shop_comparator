"""Evaluate the matching methods against labels a person made (see gold.py).

    python -m shop_comparator.matching.gold_eval                    # report (or "waiting for human labels")
    python -m shop_comparator.matching.gold_eval --reviewer 별칭     # only that reviewer's labels
    python -m shop_comparator.matching.gold_eval --json
    python -m shop_comparator.matching.gold_eval --save              # also writes evaluation_data/gold/report.json

Only labels with origin `human_ui` are used.  With no such label the report says so and prints no
performance number.  Nothing here calls a model: a method that has no stored answer for a case is
reported as "not run", never as wrong and never as "handed to a person".

Methods
  A0  original rule: identical model code, else the first search result
  A   model-code rule (codes in the product name, near matches, exactly one candidate)
  B   text score rule: brand, product name, model code scored by fixed rules.  This is NOT a language model.
  C   stored vision-model verdicts (gpt-5.4-mini, given images and product text)
  D   A first, C for what A leaves open
  B-LLM  a language model on text only: not run (needs paid calls)
"""
import json
import os
import sys

from shop_comparator.matching import gold, method_eval

METHODS = ["A0", "A", "B", "C", "D"]
NAMES = {"A0": "기존 매칭 방식(품번 일치, 없으면 첫 결과)", "A": "모델번호·규칙 기반 매칭", "B": "텍스트 점수 규칙(상품명·브랜드·모델번호, LLM 아님)",
         "C": "저장된 VLM 판정(gpt-5.4-mini)", "D": "규칙 + VLM"}
NOT_RUN = {"B-LLM": {"name": "텍스트 전용 LLM(상품명·브랜드·모델번호만 주는 언어 모델)", "status": "미실행",
                     "reason": "유료 호출이 필요하고 호출 예산이 승인되지 않았다."}}


def _entry(case, output):
    """The case in the shape the method functions expect, with candidates back in search order."""
    by_id = {c["kreamId"]: c for c in case["candidates"]}
    order = [k for k in output.get("searchOrder") or [] if k in by_id] or list(by_id)
    entry = {"goodsNo": case["musinsa"]["goodsNo"], "brand": case["musinsa"]["brand"], "name": case["musinsa"]["name"],
             "serial": case["musinsa"]["model"], "candidates": [{"kreamId": k, "name": by_id[k]["name"], "brand": by_id[k].get("brand") or "", "model": by_id[k]["model"]} for k in order],
             "vlm": {}}
    verdict = output.get("vlm")
    if verdict:
        entry["vlm"] = {verdict.get("model") or "vlm": {"decision": verdict["decision"], "kreamId": verdict["kreamId"]}}
    return entry


def answers(case, output):
    """{method: (kind, kream_id)} with kind in pick / none / human / unavailable."""
    wrapped = {"entry": _entry(case, output), "stored_vlm": bool(output.get("vlm"))}
    return {name: method_eval.RUN[name](wrapped, None) for name in METHODS}


def outcome(label, answer):
    """right_pick / wrong_pick / right_none / missed / human / unavailable for one labelled case."""
    kind, picked = answer
    if kind == "unavailable":
        return "unavailable"
    if kind == "human":
        return "human"
    if label["decision"] == "candidate":
        return "right_pick" if kind == "pick" and picked == str(label["kreamId"]) else "wrong_pick" if kind == "pick" else "missed"
    return "wrong_pick" if kind == "pick" else "right_none"          # label: no same product


def _ratio(num, den, formula):
    return {"value": round(num / den, 4) if den else None, "num": num, "den": den, "formula": formula}


def score(rows):
    """rows: [(label, outcome)] of cases with a usable human label."""
    count = {k: 0 for k in ("right_pick", "wrong_pick", "right_none", "missed", "human", "unavailable")}
    for _, result in rows:
        count[result] += 1
    run = [(label, result) for label, result in rows if result != "unavailable"]
    with_answer = [r for r in run if r[0]["decision"] == "candidate"]
    no_answer = [r for r in run if r[0]["decision"] == "none"]
    picks = count["right_pick"] + count["wrong_pick"]
    wrong_on_none = sum(1 for _, result in no_answer if result == "wrong_pick")
    return {
        "labelled": len(rows), "run": len(run), "notRun": count["unavailable"], **count,
        "precision": _ratio(count["right_pick"], picks, "맞게 고른 수 / 후보를 고른 수 (실행된 사례)"),
        "wrongPicks": {"value": count["wrong_pick"], "formula": "후보를 골랐는데 사람의 정답과 다른 수 (동일 상품 없음 사례에서 고른 것 포함)"},
        "foundRate": _ratio(count["right_pick"], len(with_answer), "맞게 고른 수 / 사람이 후보를 고른 사례 중 실행된 수"),
        "deferRate": _ratio(count["human"], len(run), "판단하지 않고 사람에게 넘긴 수 / 실행된 사례"),
        "wrongOnNone": _ratio(wrong_on_none, len(no_answer), "후보를 고른 수 / 사람이 동일 상품 없음으로 답한 사례 중 실행된 수"),
        "coverage": _ratio(len(run), len(rows), "실행된 사례 / 사람 라벨이 있는 사례"),
    }


def evaluate(reviewer=None):
    all_cases = gold.cases()
    outputs = gold.method_outputs()
    case_list = all_cases.get("cases", [])
    human = gold.latest_labels("human_ui", reviewer)
    usable = {c: l for c, l in human.items() if l["decision"] in ("candidate", "none")}
    per_case = {c["caseId"]: answers(c, outputs.get(c["caseId"], {})) for c in case_list}
    executed = {name: sum(1 for a in per_case.values() if a[name][0] != "unavailable") for name in METHODS}
    report = {
        "cases": len(case_list), "reviewer": reviewer,
        "humanLabelled": len(human), "humanUsable": len(usable),
        "humanUnsure": sum(1 for l in human.values() if l["decision"] == "unsure"),
        "automationRecords": len(gold.latest_labels("automation")),
        "labelSource": "사람이 정답 검수 화면에서 직접 저장한 라벨(origin=human_ui)만 사용",
        "execution": {name: {"name": NAMES[name], "run": executed[name], "notRun": len(case_list) - executed[name]} for name in METHODS},
        "notRunMethods": NOT_RUN,
        "sampling": {"strataCounts": all_cases.get("strataCounts"), "note": all_cases.get("selectionNote"),
                     "bias": "사례는 무작위가 아니라 기존 규칙과 VLM의 결론을 보고 네 묶음에서 고르게 뽑았다. 어려운 사례의 비중이 실제 수집분보다 높으므로 "
                             "이 표본의 비율을 전체 수집분의 정확도로 읽으면 안 된다. 묶음은 선정 기준일 뿐 정답으로 쓰지 않는다."},
    }
    if not usable:
        report["status"] = "사람 검수 대기"
        report["message"] = f"사람이 만든 정답이 {len(human)}건이라 성능 수치를 계산하지 않았다. (전체 {len(case_list)}건)"
        return report
    report["status"] = "계산됨" if len(human) >= len(case_list) else "일부 계산됨"
    report["message"] = f"사람 정답 {len(human)}/{len(case_list)}건 기준. 판단 불가 {report['humanUnsure']}건은 채점에서 뺐다."
    rows = {name: [(usable[c], outcome(usable[c], per_case[c][name])) for c in usable if c in per_case] for name in METHODS}
    report["methods"] = {name: {"name": NAMES[name], **score(rows[name])} for name in METHODS}
    common = [c for c in usable if c in per_case and all(per_case[c][name][0] != "unavailable" for name in METHODS)]
    report["commonSet"] = {"cases": len(common), "note": "다섯 방법이 모두 실행된 사례만으로 다시 계산한 값. 방법 간 비교는 이 표를 쓴다.",
                           "methods": {name: {"name": NAMES[name], **score([(usable[c], outcome(usable[c], per_case[c][name])) for c in common])}
                                       for name in METHODS}}
    strata = {c["caseId"]: c["stratum"] for c in case_list}
    report["byStratum"] = {}
    for stratum in sorted(set(strata.values())):
        ids = [c for c in usable if strata.get(c) == stratum]
        report["byStratum"][stratum] = {"labelled": len(ids), "methods": {
            name: {k: v for k, v in score([(usable[c], outcome(usable[c], per_case[c][name])) for c in ids]).items()
                   if k in ("right_pick", "wrong_pick", "right_none", "missed", "human", "unavailable")} for name in METHODS}}
    return report


def _cell(metric):
    if metric["den"] == 0:
        return "-"
    return f"{metric['num']}/{metric['den']} ({metric['value'] * 100:.1f}%)"


def render(report):
    lines = [f"평가 사례 {report['cases']}건 · 사람 정답 {report['humanLabelled']}건 (채점 가능 {report['humanUsable']}, 판단 불가 {report['humanUnsure']})"
             f" · 자동화 기록 {report['automationRecords']}건(정답으로 쓰지 않음)", f"상태: {report['status']} — {report['message']}", "",
             "[실행 범위] 저장된 결과로 답할 수 있는 사례 수 (정답과 무관)"]
    for name, e in report["execution"].items():
        lines.append(f"  {name:<3} {e['name']}: 실행 {e['run']} · 미실행 {e['notRun']}")
    for name, e in report["notRunMethods"].items():
        lines.append(f"  {name}: {e['status']} ({e['reason']})")
    if "methods" in report:
        for title, block in (("[방법별 전체] 방법마다 실행된 사례가 달라 서로 직접 비교하면 안 된다", report["methods"]),
                             (f"[공통 실행 집합 {report['commonSet']['cases']}건] 방법 간 비교용", report["commonSet"]["methods"])):
            lines += ["", title]
            for name, s in block.items():
                lines.append(f"  {name:<3} 실행 {s['run']:>2} 미실행 {s['notRun']:>2} | 정밀도 {_cell(s['precision'])} | 잘못 고름 {s['wrongPicks']['value']} | "
                             f"정답 후보 찾음 {_cell(s['foundRate'])} | 보류 {_cell(s['deferRate'])} | 없음 사례에서 잘못 고름 {_cell(s['wrongOnNone'])}")
    lines += ["", "표본: " + report["sampling"]["bias"]]
    return "\n".join(lines)


def main(argv):
    reviewer = argv[argv.index("--reviewer") + 1] if "--reviewer" in argv else None
    report = evaluate(reviewer)
    if "--save" in argv:
        with open(gold.path("report.json"), "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1) if "--json" in argv else render(report))


if __name__ == "__main__":
    main(sys.argv[1:])

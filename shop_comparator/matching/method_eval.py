"""Compare matching methods on the same frozen data, as a 1:N choice with "no same product" allowed.

    python -m shop_comparator.matching.method_eval            # report from stored results (no network)
    python -m shop_comparator.matching.method_eval build      # refresh split.json / usage.json from match_review/
    python -m shop_comparator.matching.method_eval --json

Methods (each answers one product: pick a candidate, say "none", or hand it to a person)
  A0  original rule: identical model code, else the first search result
  A   model-code rule: codes in the product name, near matches, only when exactly one candidate fits
  B   text score: brand, product name and model code only (the existing rule scorer, no model call)
  C   vision model: stored gpt-5.4-mini verdicts (it was given the images AND the names/model codes)
  D   A first, C for what A leaves open
  B-LLM  a language model on text only: NOT RUN (needs paid calls; see docs/method-evaluation.md)

Labels and where they come from
  model_code            the unique candidate carrying the Musinsa model code
  model_code_removed    the same product with that candidate taken out and only candidates with a
                        different model code left: by construction no candidate is the same item
  ai_review             a label made by an AI assistant looking at images, names and codes.
                        Not verified by a person.  Kept separate in every table.
Products without a label are not scored.
"""
import json
import os
import statistics
import sys

from shop_comparator.matching import evaluate, product_matcher
from shop_comparator.matching.match_review import code_match, musinsa_codes, norm_code, usable_code

DIR = os.path.join(evaluate.DATA_DIR, "methods")
SPLIT = os.path.join(DIR, "split.json")
USAGE = os.path.join(DIR, "usage.json")
REPORT = os.path.join(DIR, "report.json")
METHODS = ["A0", "A", "B", "C", "D"]
METHOD_NAMES = {"A0": "기존 규칙(품번 일치, 없으면 첫 결과)", "A": "모델번호·문자열 규칙", "B": "텍스트 점수(상품명·브랜드·모델번호)",
                "C": "VLM(이미지+상품 정보, gpt-5.4-mini 저장 결과)", "D": "규칙 + VLM"}
PRICES = {"gpt-5.4-mini": (0.75, 4.50)}       # USD per 1M tokens (input, output), list price checked 2026-10-09
KRW_PER_USD = 1400


def _codes(model):
    return {c for c in (norm_code(part) for part in str(model or "").replace(",", "/").split("/")) if usable_code(c)}


def family(entry, truth_id=""):
    """Products that are close variants share a family: same brand and the same code stem."""
    candidate = next((c for c in entry["candidates"] if c["kreamId"] == truth_id), None)
    code = next(iter(sorted(_codes(candidate["model"]))), "") if candidate else ""
    if not code:
        code = next((norm_code(c) for c in musinsa_codes(entry) if usable_code(norm_code(c))), "")
    stem = code[:6] if code else " ".join(str(entry.get("name") or "").split()[:2]).lower()
    return f"{str(entry.get('brand') or '').strip().lower()}|{stem}"


def cases(dataset):
    """Scorable cases built from the frozen dataset."""
    out = []
    for goods_no, entry in dataset["products"].items():
        kream_id, decision = evaluate.model_code_pick(entry)
        review = entry.get("review") or {}
        base = {"goodsNo": goods_no, "entry": entry, "family": family(entry, kream_id or str(review.get("kreamId") or ""))}
        if decision == "model_code":
            out.append({**base, "kind": "answer", "truth": kream_id, "source": "model_code", "stored_vlm": True})
            confirmed = _codes(next(c for c in entry["candidates"] if c["kreamId"] == kream_id)["model"])
            rest = [c for c in entry["candidates"] if c["kreamId"] != kream_id and _codes(c["model"]) and not (_codes(c["model"]) & confirmed)
                    and not any(code_match(code, c["model"]) for code in musinsa_codes(entry))]
            if rest:
                out.append({**base, "kind": "none", "truth": "", "source": "model_code_removed", "stored_vlm": False,
                            "entry": {**entry, "candidates": rest}})
        elif review.get("label") == "same":
            out.append({**base, "kind": "answer", "truth": str(review["kreamId"]), "source": "ai_review", "stored_vlm": True})
        elif review.get("label") == "none":
            out.append({**base, "kind": "none", "truth": "", "source": "ai_review", "stored_vlm": True})
        elif review.get("label") == "not":
            out.append({**base, "kind": "rejected", "truth": str(review["kreamId"]), "source": "ai_review", "stored_vlm": True})
    return out


# ---- methods: return ("pick", id) | ("none", "") | ("human", "") | ("unavailable", "")
def method_a0(case, dataset):
    picked, how = evaluate.legacy_pick(case["entry"])
    return ("pick", picked) if how != "none" else ("none", "")


def method_a(case, dataset):
    picked, decision = evaluate.model_code_pick(case["entry"])
    return ("pick", picked) if decision == "model_code" else ("human", "")       # the rule has no evidence either way


def method_b(case, dataset):
    entry = case["entry"]
    musinsa = {"brand": entry.get("brand"), "name": entry.get("name"), "serial_number": entry.get("serial")}
    candidates = [{"id": c["kreamId"], "brand": c.get("brand") or "", "name_ko": c.get("name") or "",
                   "all_text": f"{c.get('name') or ''} {c.get('model') or ''}"} for c in entry["candidates"]]
    decision = product_matcher.local_match_decision(musinsa, candidates)
    if decision.matched:
        return "pick", str(decision.product["id"])
    return ("human", "") if decision.method == "local_uncertain" else ("none", "")


def method_c(case, dataset):
    if not case["stored_vlm"]:
        return "unavailable", ""                 # no stored verdict for a modified candidate list; not run
    verdict = next(iter((case["entry"].get("vlm") or {}).values()), None)
    if not verdict:
        return "unavailable", ""
    if verdict["decision"] == "same":
        return "pick", str(verdict["kreamId"])
    return ("none", "") if verdict["decision"] == "different" else ("human", "")


def method_d(case, dataset):
    first = method_a(case, dataset)
    return first if first[0] == "pick" else method_c(case, dataset)


RUN = {"A0": method_a0, "A": method_a, "B": method_b, "C": method_c, "D": method_d}


def outcome(case, answer):
    """right_pick / wrong_pick / right_none / missed / human / unavailable / unknown."""
    kind, picked = answer
    if kind == "unavailable":
        return "unavailable"
    if kind == "human":
        return "human"
    if case["kind"] == "answer":
        return "right_pick" if kind == "pick" and picked == case["truth"] else "wrong_pick" if kind == "pick" else "missed"
    if case["kind"] == "none":
        return "wrong_pick" if kind == "pick" else "right_none"
    # "rejected": only the rejected candidate is known to be wrong
    return "wrong_pick" if kind == "pick" and picked == case["truth"] else "unknown"


def _rate(part, whole):
    return round(part / whole, 4) if whole else None


def score(rows):
    """rows: [(case, outcome)].  Every ratio carries its numerator and denominator."""
    count = {k: 0 for k in ("right_pick", "wrong_pick", "right_none", "missed", "human", "unavailable", "unknown")}
    for _, result in rows:
        count[result] += 1
    evaluated = len(rows) - count["unavailable"]
    answers = sum(1 for case, result in rows if case["kind"] == "answer" and result != "unavailable")
    nones = sum(1 for case, result in rows if case["kind"] == "none" and result != "unavailable")
    picks = count["right_pick"] + count["wrong_pick"]
    decided = picks + count["right_none"] + count["missed"]
    wrong_on_none = sum(1 for case, result in rows if case["kind"] == "none" and result == "wrong_pick")
    return {
        "cases": len(rows), "evaluated": evaluated, **count,
        "precision": {"value": _rate(count["right_pick"], picks), "num": count["right_pick"], "den": picks,
                      "formula": "맞은 동일 판정 / 정오를 아는 동일 판정"},
        "recall": {"value": _rate(count["right_pick"], answers), "num": count["right_pick"], "den": answers,
                   "formula": "맞은 동일 판정 / 정답이 있는 평가 대상"},
        "false_match": {"value": _rate(count["wrong_pick"], picks), "num": count["wrong_pick"], "den": picks,
                        "formula": "틀린 동일 판정 / 정오를 아는 동일 판정"},
        "false_match_on_none": {"value": _rate(wrong_on_none, nones), "num": wrong_on_none, "den": nones,
                                "formula": "동일 상품이 없는데 고른 수 / 동일 상품이 없는 평가 대상"},
        "decided": {"value": _rate(decided, evaluated), "num": decided, "den": evaluated, "formula": "(동일 판정 + 없음 판정) / 평가 대상"},
        "to_human": {"value": _rate(count["human"], evaluated), "num": count["human"], "den": evaluated, "formula": "사람에게 넘긴 수 / 평가 대상"},
    }


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def split_of(all_cases, split):
    """'dev' when the product or a close variant was seen while the rules were written, else 'eval'."""
    dev_ids = set(split.get("dev_ids") or [])
    dev_families = {c["family"] for c in all_cases if c["goodsNo"] in dev_ids}
    return {id(c): "dev" if c["goodsNo"] in dev_ids or c["family"] in dev_families else "eval" for c in all_cases}, dev_families


def timing(usage):
    seconds = sorted(u["seconds"] for u in usage.values() if u.get("seconds") is not None)
    if not seconds:
        return None
    tokens_in = sum(u.get("input_tokens") or 0 for u in usage.values())
    tokens_out = sum(u.get("output_tokens") or 0 for u in usage.values())
    model = next((u.get("model") for u in usage.values() if u.get("model")), "")
    price = next((p for name, p in PRICES.items() if model.startswith(name)), None)
    usd = (tokens_in * price[0] + tokens_out * price[1]) / 1_000_000 if price else None
    return {"calls": len(usage), "model": model, "seconds_median": round(statistics.median(seconds), 2),
            "seconds_p95": round(seconds[min(len(seconds) - 1, int(len(seconds) * 0.95))], 2),
            "input_tokens": tokens_in, "output_tokens": tokens_out,
            "cost_usd": round(usd, 4) if usd is not None else None,
            "cost_krw_per_product": round(usd * KRW_PER_USD / len(usage), 2) if usd is not None else None}


def evaluate_all(dataset, split, usage):
    all_cases = cases(dataset)
    part, dev_families = split_of(all_cases, split)
    report = {"products": len(dataset["products"]), "cases": len(all_cases),
              "case_counts": {}, "split": {"dev": 0, "eval": 0, "dev_ids": len(split.get("dev_ids") or []), "dev_families": len(dev_families),
                                           "moved_to_dev_by_family": 0}, "methods": {}, "examples": {}}
    dev_ids = set(split.get("dev_ids") or [])
    for case in all_cases:
        key = f"{case['kind']}/{case['source']}"
        report["case_counts"][key] = report["case_counts"].get(key, 0) + 1
        report["split"][part[id(case)]] += 1
        if part[id(case)] == "dev" and case["goodsNo"] not in dev_ids:
            report["split"]["moved_to_dev_by_family"] += 1
    for name in METHODS:
        rows = [(case, outcome(case, RUN[name](case, dataset))) for case in all_cases]
        by_source = {}
        for case, result in rows:
            by_source.setdefault(f"{case['kind']}/{case['source']}", []).append((case, result))
        report["methods"][name] = {
            "name": METHOD_NAMES[name], "all": score(rows),
            "eval": score([r for r in rows if part[id(r[0])] == "eval"]),
            "dev": score([r for r in rows if part[id(r[0])] == "dev"]),
            "by_label": {key: score(value) for key, value in by_source.items()},
        }
        report["examples"][name] = [
            {"goodsNo": case["goodsNo"], "name": case["entry"].get("name"), "serial": case["entry"].get("serial"), "kind": case["kind"],
             "source": case["source"], "truth": case["truth"], "answer": list(RUN[name](case, dataset)), "result": result}
            for case, result in rows if result in ("wrong_pick", "missed")][:12]
    report["methods"]["B-LLM"] = {"name": "언어 모델의 텍스트 판단(상품명·브랜드·모델번호만)", "status": "미실행",
                                  "reason": "유료 호출이 필요하다. 호출 한도를 받은 뒤 실행한다."}
    report["vlm_usage"] = timing(usage)
    return report


def build():
    """Freeze the development ids and the stored per-call usage (new files; the earlier evaluation files are untouched)."""
    import csv
    from shop_comparator.matching import match_review
    os.makedirs(DIR, exist_ok=True)
    dev_ids = []
    try:
        with open(os.path.join(match_review.ROOT, "verdicts.csv"), encoding="utf-8-sig") as f:
            dev_ids = [row["musinsa_goods_no"] for row in csv.DictReader(f)]
    except OSError:
        pass
    with open(SPLIT, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"rule": "개발용 = 규칙과 프롬프트를 만들 때 본 첫 100개 상품(2026-10-09 1차 검토 대상)과, 그 상품과 브랜드·모델번호 앞 6자가 "
                           "같은 변형 상품. 평가용 = 나머지.", "dev_ids": dev_ids}, f, ensure_ascii=False, indent=1)
    verdicts = _load(os.path.join(match_review.ROOT, "vlm_results.json"), {})
    usage = {g: {"model": (v.get("usage") or {}).get("model"), "input_tokens": (v.get("usage") or {}).get("input_tokens"),
                 "output_tokens": (v.get("usage") or {}).get("output_tokens"), "seconds": v.get("seconds"), "at": v.get("at")}
             for g, v in verdicts.items() if v.get("decision") in ("same", "different", "uncertain")}
    with open(USAGE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(usage, f, ensure_ascii=False, indent=1)
    return len(dev_ids), len(usage)


def _line(name, s):
    def cell(m):
        return f"{m['num']}/{m['den']}" + (f" ({m['value'] * 100:.1f}%)" if m["value"] is not None else "")
    return (f"  {name:<3} 평가 {s['evaluated']:>3} | 정밀도 {cell(s['precision']):<18} 재현율 {cell(s['recall']):<18} "
            f"오매칭 {s['wrong_pick']:>3} | 없음 정답 {s['right_none']:>3} 놓침 {s['missed']:>3} 사람에게 {cell(s['to_human']):<16} 미실행 {s['unavailable']}")


def render(report):
    lines = [f"상품 {report['products']}개에서 만든 평가 사례 {report['cases']}개", "  " + ", ".join(f"{k} {v}" for k, v in report["case_counts"].items()),
             f"  개발용 {report['split']['dev']} / 평가용 {report['split']['eval']} (변형 상품이라 개발용으로 옮긴 사례 {report['split']['moved_to_dev_by_family']})", ""]
    for part, title in (("all", "전체"), ("eval", "평가용만")):
        lines.append(f"[{title}]")
        lines += [_line(name, report["methods"][name][part]) for name in METHODS]
        lines.append("")
    lines.append("[라벨 출처별: 동일 판정 맞음/틀림, 없음 맞음, 놓침, 사람에게]")
    for name in METHODS:
        lines.append(f"  {name}: " + " | ".join(f"{key} {s['right_pick']}/{s['wrong_pick']}/{s['right_none']}/{s['missed']}/{s['human']}"
                                                 for key, s in report["methods"][name]["by_label"].items()))
    lines += ["", f"  B-LLM: {report['methods']['B-LLM']['status']} ({report['methods']['B-LLM']['reason']})"]
    usage = report.get("vlm_usage")
    if usage:
        lines.append(f"  VLM 저장 호출 {usage['calls']}건 ({usage['model']}): 중앙값 {usage['seconds_median']}초, p95 {usage['seconds_p95']}초, "
                     f"입력 {usage['input_tokens']:,} / 출력 {usage['output_tokens']:,} 토큰, 상품당 약 {usage['cost_krw_per_product']}원")
    return "\n".join(lines)


def main(argv):
    if argv[:1] == ["build"]:
        print("dev ids, usage rows:", build())
    report = evaluate_all(evaluate.load(), _load(SPLIT, {}), _load(USAGE, {}))
    if "--save" in argv or argv[:1] == ["build"]:
        os.makedirs(DIR, exist_ok=True)
        with open(REPORT, "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1) if "--json" in argv else render(report))


if __name__ == "__main__":
    main(sys.argv[1:])

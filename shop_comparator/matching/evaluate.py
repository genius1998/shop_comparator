"""How much better is the current matching than the original rule?

Original rule (desktop app): search KREAM with the Musinsa model code, take the
result whose model code is identical, otherwise take the first result.

Current pipeline: model-code check (also codes written in the product name,
near matches, and only when exactly one candidate fits) -> vision model on the
rest -> a person confirms.

The evaluation runs on a frozen dataset (docs/evaluation/matching_eval.json),
so it needs no network and gives the same numbers every time.

    python -m shop_comparator.matching.evaluate            # print the report
    python -m shop_comparator.matching.evaluate build      # rebuild the dataset from match_review/
"""
import csv
import json
import os
import sys

from shop_comparator.matching.match_review import code_match, musinsa_codes

DATA_DIR = os.path.join("docs", "evaluation")
DATASET = os.path.join(DATA_DIR, "matching_eval.json")
REVIEW = os.path.join(DATA_DIR, "review_labels.csv")
# result files written by vlm_match.py, in the order they are reported
VLM_FILES = ["vlm_results.json", "vlm_results_local.json"]


def legacy_pick(entry):
    """Original rule -> (kream_id, 'exact' | 'first' | 'none')."""
    candidates = entry["candidates"]
    for c in candidates:
        if code_match(entry.get("serial"), c.get("model")) == "exact":
            return str(c["kreamId"]), "exact"
    if candidates:
        return str(candidates[0]["kreamId"]), "first"
    return "", "none"


def model_code_pick(entry):
    """Current model-code rule -> (kream_id, 'model_code' | 'ambiguous' | 'no_code')."""
    hits = []
    for c in entry["candidates"]:
        kinds = [code_match(code, c.get("model")) for code in musinsa_codes(entry)]
        kind = "exact" if "exact" in kinds else "near" if "near" in kinds else None
        if kind:
            hits.append((str(c["kreamId"]), kind))
    best = [h for h in hits if h[1] == "exact"] or hits
    if len(best) == 1:
        return best[0][0], "model_code"
    return "", "ambiguous" if best else "no_code"


def judge(entry, picked):
    """Is `picked` the right KREAM product?  True / False / None (nobody checked).

    A unique model-code match is taken as correct (the code is the maker's own
    identifier).  Everything else needs a reviewed label:
      same      this KREAM product is the same item
      none      no candidate is the same item
      not       this KREAM product is a different item (others not reviewed)
      doubtful  the reviewer could not tell from the thumbnails
    """
    kream_id, decision = model_code_pick(entry)
    if decision == "model_code":
        return picked == kream_id
    review = entry.get("review") or {}
    label, reviewed_id = review.get("label"), str(review.get("kreamId") or "")
    if label == "same":
        return picked == reviewed_id
    if label == "none":
        return False
    if label == "not" and picked == reviewed_id:
        return False
    return None


def evaluate_vlm(rows, model):
    """One vision model against the model-code matches and the reviewed labels."""
    out = {"judged": 0, "seconds": 0.0,
           "code_checked": 0, "code_agree": 0, "code_other": 0, "code_not_same": 0,
           "open": 0, "open_same": 0, "open_same_right": 0, "open_same_wrong": 0, "open_same_unreviewed": 0,
           "open_different": 0, "open_uncertain": 0, "found_ids": []}
    for e in rows:
        verdict = (e.get("vlm") or {}).get(model)
        if not verdict:
            continue
        out["judged"] += 1
        out["seconds"] += verdict.get("seconds") or 0
        kream_id, decision = model_code_pick(e)
        picked = str(verdict.get("kreamId") or "")
        if decision == "model_code":
            out["code_checked"] += 1
            if verdict["decision"] != "same":
                out["code_not_same"] += 1
            elif picked == kream_id:
                out["code_agree"] += 1
            else:
                out["code_other"] += 1
            continue
        out["open"] += 1
        if verdict["decision"] == "same":
            out["open_same"] += 1
            right = judge(e, picked)
            out["open_same_unreviewed" if right is None else "open_same_right" if right else "open_same_wrong"] += 1
            if right:
                out["found_ids"].append(e["goodsNo"])
        else:
            out["open_different" if verdict["decision"] == "different" else "open_uncertain"] += 1
    out["seconds_each"] = round(out["seconds"] / out["judged"], 1) if out["judged"] else None
    del out["seconds"]
    return out


def evaluate(dataset):
    rows = list(dataset["products"].values())
    legacy = {"exact": 0, "first": 0, "none": 0, "first_right": 0, "first_wrong": 0, "first_unreviewed": 0}
    kinds = {"model_code": 0, "ambiguous": 0, "no_code": 0}
    for e in rows:
        picked, how = legacy_pick(e)
        legacy[how] += 1
        if how == "first":
            right = judge(e, picked)
            legacy["first_unreviewed" if right is None else "first_right" if right else "first_wrong"] += 1
        kinds[model_code_pick(e)[1]] += 1
    models = dataset.get("vlm_models") or []
    vlm = {model: evaluate_vlm(rows, model) for model in models}
    reviewed = sum(1 for e in rows if model_code_pick(e)[1] != "model_code" and (e.get("review") or {}).get("label") == "same")
    return {"products": len(rows), "legacy": legacy, "model_code": kinds, "vlm": vlm,
            "found": {"legacy_exact": legacy["exact"], "model_code": kinds["model_code"],
                      "model_code_plus_reviewed": kinds["model_code"] + reviewed}}


def _pct(part, whole):
    return f"{part / whole * 100:.1f}%" if whole else "-"


def render(report):
    n, legacy, kinds, found = report["products"], report["legacy"], report["model_code"], report["found"]
    lines = [
        f"상품 {n}개 (무신사 신발 랭킹, KREAM 검색 후보가 있는 상품)",
        "",
        "[1] 기존 규칙: 검색 결과에서 품번이 같은 상품, 없으면 첫 번째 결과",
        f"  품번이 같은 상품을 찾음        {legacy['exact']:4d}  ({_pct(legacy['exact'], n)})",
        f"  못 찾아 첫 번째 결과를 사용    {legacy['first']:4d}  ({_pct(legacy['first'], n)})",
        f"    그중 실제로 같은 상품        {legacy['first_right']:4d}",
        f"    그중 다른 상품(잘못된 매칭)  {legacy['first_wrong']:4d}",
        f"    그중 확인하지 않음           {legacy['first_unreviewed']:4d}",
        "",
        "[2] 현재 모델번호 대조 (상품명 속 모델번호, 접미 차이 허용, 후보가 하나일 때만 확정)",
        f"  확정                           {kinds['model_code']:4d}  ({_pct(kinds['model_code'], n)})",
        f"  같은 모델번호 후보가 여러 개   {kinds['ambiguous']:4d}",
        f"  모델번호로 판단 불가           {kinds['no_code']:4d}",
        "",
        "[3] 이미지 판정 모델",
    ]
    for model, v in report["vlm"].items():
        lines += [
            f"  {model}: {v['judged']}개 판정, 상품당 {v['seconds_each']}초",
            f"    모델번호로 확정된 {v['code_checked']}개에서 같은 상품을 고름 {v['code_agree']} ({_pct(v['code_agree'], v['code_checked'])}), "
            f"다른 후보 {v['code_other']}, 동일 아님 {v['code_not_same']}",
            f"    모델번호로 확정 못 한 {v['open']}개: 동일 {v['open_same']} (확인 결과 맞음 {v['open_same_right']}, 틀림 {v['open_same_wrong']}, "
            f"미확정 {v['open_same_unreviewed']}), 다름 {v['open_different']}, 불확실 {v['open_uncertain']}",
        ]
    lines += [
        "",
        "[4] 같은 상품을 찾은 수",
        f"  기존 규칙(품번 일치)                     {found['legacy_exact']:4d}  ({_pct(found['legacy_exact'], n)})",
        f"  모델번호 대조                            {found['model_code']:4d}  ({_pct(found['model_code'], n)})",
        f"  모델번호 대조 + VLM 제안 중 AI 보조 검토 채택분 {found['model_code_plus_reviewed']:4d}  ({_pct(found['model_code_plus_reviewed'], n)})",
    ]
    return "\n".join(lines)


def build():
    """Freeze the current match_review/ data into the tracked dataset (no images, no prices)."""
    from shop_comparator.matching import match_review

    data = match_review.load()
    review = {}
    if os.path.exists(REVIEW):
        with open(REVIEW, encoding="utf-8-sig", newline="") as f:
            review = {row["musinsa_goods_no"]: row for row in csv.DictReader(f)}
    products = {}
    for goods_no, entry in data.items():
        if not entry.get("candidates"):
            continue
        products[goods_no] = {
            "goodsNo": goods_no, "brand": entry.get("brand"), "name": entry.get("name"), "serial": entry.get("serial"),
            "candidates": [{"kreamId": str(c["kreamId"]), "name": c.get("name"), "model": c.get("model") or ""} for c in entry["candidates"]],
            "vlm": {},
        }
        if goods_no in review:
            r = review[goods_no]
            products[goods_no]["review"] = {"label": r["label"], "kreamId": r["kream_id"], "note": r["note"]}
    models = []
    for name in VLM_FILES:
        try:
            with open(os.path.join(match_review.ROOT, name), encoding="utf-8") as f:
                verdicts = json.load(f)
        except (OSError, ValueError):
            continue
        for goods_no, verdict in verdicts.items():
            if goods_no not in products or verdict.get("decision") not in ("same", "different", "uncertain"):
                continue
            model = (verdict.get("usage") or {}).get("model") or name
            if model not in models:
                models.append(model)
            products[goods_no]["vlm"][model] = {"decision": verdict["decision"], "kreamId": str(verdict.get("kream_id") or ""),
                                               "confidence": verdict.get("confidence"), "seconds": verdict.get("seconds")}
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DATASET, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"vlm_models": models, "products": products}, f, ensure_ascii=False, indent=1)
    return len(products)


def load():
    with open(DATASET, encoding="utf-8") as f:
        return json.load(f)


def main(argv):
    if argv[:1] == ["build"]:
        print("products:", build())
    report = evaluate(load())
    print(render(report))
    if "--json" in argv:
        print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])

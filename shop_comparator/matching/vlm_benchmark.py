"""Benchmark of vision models on one task: are these two product images the same item?

Every model gets the same labelled image pairs and nothing else (no product
name, no model code), so the numbers measure image judgement only.

  pairs    docs/evaluation/vlm_pairs.json       (frozen; built from match_review/)
  results  docs/evaluation/vlm_benchmark.json   (one verdict per pair per model)

    python -m shop_comparator.matching.vlm_benchmark pairs                 # build the pair list
    python -m shop_comparator.matching.vlm_benchmark run gpt-5.4-mini      # OpenAI model (paid)
    python -m shop_comparator.matching.vlm_benchmark run remote            # self-hosted model behind VLM_REMOTE_COMMAND
    python -m shop_comparator.matching.vlm_benchmark                       # print the report

Backends
  OpenAI   key OPENAI_API_KEY_VLM.  Token usage is recorded per call.
  remote   VLM_REMOTE_COMMAND is a program that takes `--image-a PATH --image-b PATH`
           and prints JSON with `result` (SAME / DIFFERENT / UNCERTAIN) and `confidence`.
           VLM_REMOTE_MODEL names the model in the report.  The benchmark runs it
           one call at a time and stops after two failures in a row.
Labels
  code_same      the KREAM product is the unique model-code match            -> same
  reviewed_same  a reviewer confirmed the pair (model codes differ or absent) -> same
  other_model    another search result with a different model code           -> different
  reviewed_not   a reviewer rejected the pair                                 -> different
"""
import json
import os
import random
import subprocess
import sys
import time

import requests

from shop_comparator.matching import evaluate, match_review, vlm_match
from shop_comparator.matching.match_review import code_match, musinsa_codes, norm_code, usable_code

PAIRS = os.path.join(evaluate.DATA_DIR, "vlm_pairs.json")
RESULTS = os.path.join(evaluate.DATA_DIR, "vlm_benchmark.json")
SEED = 20261010
# USD per 1M tokens (input, output), public list prices checked 2026-10-09; KRW per USD used for the report
PRICES = {"gpt-5.4-nano": (0.20, 1.25), "gpt-5.4-mini": (0.75, 4.50)}
KRW_PER_USD = 1400
PAIR_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"result": {"type": "string", "enum": ["same", "different", "uncertain"]},
                   "confidence": {"type": "number"}, "reason": {"type": "string"}},
    "required": ["result", "confidence", "reason"],
}
PAIR_TASK = "Image A and image B are product photos from two shops. Are they the same product (same model and colorway)?"


def build_pairs(dataset, images, per_kind=36, seed=SEED):
    """Pick labelled pairs.  `images` maps (goods_no, kream_id) -> True when both files exist."""
    rng = random.Random(seed)
    coded, others, reviewed = [], [], []
    for goods_no, entry in dataset["products"].items():
        kream_id, decision = evaluate.model_code_pick(entry)
        by_id = {c["kreamId"]: c for c in entry["candidates"]}
        if decision == "model_code":
            if images(goods_no, kream_id):
                coded.append({"goodsNo": goods_no, "kreamId": kream_id, "label": "same", "kind": "code_same"})
            confirmed = {norm_code(part) for part in str(by_id[kream_id]["model"]).replace(",", "/").split("/")}
            for c in entry["candidates"]:
                parts = {norm_code(part) for part in str(c.get("model") or "").replace(",", "/").split("/")}
                parts = {p for p in parts if usable_code(p)}
                if c["kreamId"] == kream_id or not parts or parts & confirmed:
                    continue
                if any(code_match(code, c["model"]) for code in musinsa_codes(entry)):
                    continue
                if images(goods_no, c["kreamId"]):
                    others.append({"goodsNo": goods_no, "kreamId": c["kreamId"], "label": "different", "kind": "other_model"})
            continue
        review = entry.get("review") or {}
        if review.get("label") in ("same", "not") and images(goods_no, review["kreamId"]):
            reviewed.append({"goodsNo": goods_no, "kreamId": review["kreamId"],
                             "label": "same" if review["label"] == "same" else "different",
                             "kind": "reviewed_same" if review["label"] == "same" else "reviewed_not"})
    positives = sum(1 for p in reviewed if p["label"] == "same")
    negatives = len(reviewed) - positives
    pairs = (rng.sample(coded, min(len(coded), per_kind)) + reviewed
             + rng.sample(others, min(len(others), per_kind + positives - negatives)))
    for p in pairs:
        entry = dataset["products"][p["goodsNo"]]
        candidate = next(c for c in entry["candidates"] if c["kreamId"] == p["kreamId"])
        p.update(musinsa=f"{entry.get('brand')} {entry.get('name')}", musinsaCode=entry.get("serial"),
                 kream=candidate.get("name"), kreamCode=candidate.get("model"))
    return pairs


def image_paths(pair):
    data = match_review.load()[pair["goodsNo"]]
    candidate = next(c for c in data["candidates"] if str(c["kreamId"]) == pair["kreamId"])
    return (os.path.abspath(os.path.join(match_review.ROOT, data["musinsaImage"])),
            os.path.abspath(os.path.join(match_review.ROOT, candidate["image"])))


def ask_openai(model, image_a, image_b, timeout=90):
    key = os.getenv("OPENAI_API_KEY_VLM")
    if not key:
        raise RuntimeError(".env에 OPENAI_API_KEY_VLM이 없습니다.")

    def data_url(path):
        import base64
        with open(path, "rb") as f:
            return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode("ascii")

    content = [{"type": "input_text", "text": PAIR_TASK},
               {"type": "input_text", "text": "Image A"}, {"type": "input_image", "image_url": data_url(image_a), "detail": "low"},
               {"type": "input_text", "text": "Image B"}, {"type": "input_image", "image_url": data_url(image_b), "detail": "low"}]
    payload = {"model": model, "input": [{"role": "system", "content": vlm_match.SYSTEM}, {"role": "user", "content": content}],
               "text": {"format": {"type": "json_schema", "name": "pair_match", "strict": True, "schema": PAIR_SCHEMA}},
               "reasoning": {"effort": "low"}, "max_output_tokens": 500, "store": False}
    response = requests.post("https://api.openai.com/v1/responses", json=payload, timeout=timeout,
                             headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    if response.status_code != 200:
        raise RuntimeError(f"OpenAI API {response.status_code}")
    body = response.json()
    text = "".join(part.get("text", "") for item in body.get("output", []) for part in item.get("content", []) or []
                   if part.get("type") == "output_text")
    answer, usage = json.loads(text), body.get("usage") or {}
    return {"result": answer["result"], "confidence": answer.get("confidence"), "reason": answer.get("reason", "")[:200],
            "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"), "model": body.get("model")}


def ask_remote(image_a, image_b, timeout=300):
    command = os.getenv("VLM_REMOTE_COMMAND")
    if not command:
        raise RuntimeError("VLM_REMOTE_COMMAND가 설정되지 않았습니다.")
    argv = (["cmd", "/c", command] if command.lower().endswith((".bat", ".cmd")) else [command]) + ["--image-a", image_a, "--image-b", image_b]
    done = subprocess.run(argv, capture_output=True, timeout=timeout)
    out = done.stdout.decode("utf-8", errors="replace")
    start = out.find("{")
    if done.returncode != 0 or start < 0:
        raise RuntimeError(f"원격 모델 호출 실패 (exit {done.returncode})")
    answer = json.loads(out[start:out.rfind("}") + 1])
    raw = str(answer.get("result", "")).lower()
    # "same model, different variant" (colour or material) is a different product for price comparison
    result = "different" if raw == "same_model_different_variant" else raw
    if result not in ("same", "different", "uncertain"):
        raise RuntimeError("원격 모델 응답이 정해진 형식이 아닙니다.")
    return {"result": result, "raw_result": raw, "confidence": answer.get("confidence"), "reason": str(answer.get("reason", ""))[:200],
            "latency_ms": answer.get("latency_ms"), "model": os.getenv("VLM_REMOTE_MODEL") or "remote"}


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=1)


def run(backend, limit=None):
    """Judge every pair that has no verdict from this model yet (resumable)."""
    pairs = _load(PAIRS, [])
    results = _load(RESULTS, {})
    label = (os.getenv("VLM_REMOTE_MODEL") or "remote") if backend == "remote" else backend
    mine = results.setdefault(label, {})
    failures = done = 0
    for pair in pairs:
        key = f"{pair['goodsNo']}:{pair['kreamId']}"
        if key in mine:
            continue
        if limit is not None and done >= limit:
            break
        image_a, image_b = image_paths(pair)
        started = time.time()
        try:
            verdict = ask_remote(image_a, image_b) if backend == "remote" else ask_openai(backend, image_a, image_b)
        except Exception as exc:
            failures += 1
            print(key, "error", str(exc)[:120], flush=True)
            if failures >= 2:
                print("연속 실패로 중단합니다.", flush=True)
                break
            continue
        failures = 0
        done += 1
        verdict["seconds"] = round(time.time() - started, 1)
        mine[key] = verdict
        _save(RESULTS, results)
        print(key, pair["label"], "->", verdict["result"], verdict.get("confidence"), f"{verdict['seconds']}s", flush=True)
    return results


def score(pairs, verdicts):
    """Counts for one model.  'uncertain' is neither a correct 'same' nor a correct 'different'."""
    out = {"pairs": 0, "correct": 0, "uncertain": 0, "tp": 0, "fp": 0, "fn": 0, "tn": 0, "seconds": [], "by_kind": {},
           "input_tokens": 0, "output_tokens": 0, "wrong_same_confidence": [], "right_same_confidence": []}
    for pair in pairs:
        verdict = verdicts.get(f"{pair['goodsNo']}:{pair['kreamId']}")
        if not verdict:
            continue
        out["pairs"] += 1
        result, label = verdict["result"], pair["label"]
        kind = out["by_kind"].setdefault(pair["kind"], {"pairs": 0, "correct": 0, "uncertain": 0})
        kind["pairs"] += 1
        if result == "uncertain":
            out["uncertain"] += 1
            kind["uncertain"] += 1
        if result == label:
            out["correct"] += 1
            kind["correct"] += 1
        if label == "same":
            out["tp" if result == "same" else "fn"] += 1
        else:
            out["fp" if result == "same" else "tn"] += 1
        if result == "same" and verdict.get("confidence") is not None:
            out["right_same_confidence" if label == "same" else "wrong_same_confidence"].append(verdict["confidence"])
        out["seconds"].append(verdict.get("seconds") or 0)
        out["input_tokens"] += verdict.get("input_tokens") or 0
        out["output_tokens"] += verdict.get("output_tokens") or 0
    n = out["pairs"]
    out["accuracy"] = out["correct"] / n if n else None
    out["precision"] = out["tp"] / (out["tp"] + out["fp"]) if out["tp"] + out["fp"] else None
    out["recall"] = out["tp"] / (out["tp"] + out["fn"]) if out["tp"] + out["fn"] else None
    out["false_same_rate"] = out["fp"] / (out["fp"] + out["tn"]) if out["fp"] + out["tn"] else None
    seconds = sorted(out.pop("seconds"))
    out["seconds_median"] = seconds[len(seconds) // 2] if seconds else None
    return out


def cost_krw(model, summary):
    """API cost per pair in KRW, or None when the model has no list price (self-hosted)."""
    price = next((p for name, p in PRICES.items() if model.startswith(name)), None)
    if not price or not summary["pairs"]:
        return None
    usd = (summary["input_tokens"] * price[0] + summary["output_tokens"] * price[1]) / 1_000_000
    return usd * KRW_PER_USD / summary["pairs"]


def report():
    pairs, results = _load(PAIRS, []), _load(RESULTS, {})
    out = {}
    for model, verdicts in results.items():
        summary = score(pairs, verdicts)
        out[model] = {**summary, "cost_krw": cost_krw(model, summary)}
    return out


def _fmt(value, pct=True):
    if value is None:
        return "-"
    return f"{value * 100:.1f}%" if pct else f"{value:.2f}"


def render(summary):
    pairs = _load(PAIRS, [])
    kinds = {}
    for p in pairs:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    lines = [f"이미지 쌍 {len(pairs)}개: " + ", ".join(f"{k} {v}" for k, v in kinds.items()), ""]
    for model, s in summary.items():
        cost = "API 비용 없음(자체 호스팅)" if s["cost_krw"] is None else f"쌍당 약 {s['cost_krw']:.2f}원"
        lines += [
            f"{model}  ({s['pairs']}쌍)",
            f"  정확도 {_fmt(s['accuracy'])}   동일 판정 정밀도 {_fmt(s['precision'])}   재현율 {_fmt(s['recall'])}   "
            f"다른 상품을 동일로 판정 {_fmt(s['false_same_rate'])}   불확실 {s['uncertain']}",
            f"  TP {s['tp']}  FP {s['fp']}  FN {s['fn']}  TN {s['tn']}   응답 시간 중앙값 {s['seconds_median']}초   {cost}",
            "  " + ", ".join(f"{k} {v['correct']}/{v['pairs']}" for k, v in s["by_kind"].items()),
        ]
        if s["wrong_same_confidence"]:
            lines.append(f"  틀린 '동일'의 신뢰도 {min(s['wrong_same_confidence'])}~{max(s['wrong_same_confidence'])}, "
                         f"맞은 '동일'의 신뢰도 {min(s['right_same_confidence'], default='-')}~{max(s['right_same_confidence'], default='-')}")
        lines.append("")
    return "\n".join(lines)


def main(argv):
    if argv[:1] == ["pairs"]:
        data = match_review.load()

        def have(goods_no, kream_id):
            entry = data.get(goods_no) or {}
            candidate = next((c for c in entry.get("candidates", []) if str(c["kreamId"]) == str(kream_id)), None)
            return bool(candidate and candidate.get("image") and entry.get("musinsaImage")
                        and os.path.exists(os.path.join(match_review.ROOT, candidate["image"]))
                        and os.path.exists(os.path.join(match_review.ROOT, entry["musinsaImage"])))

        pairs = build_pairs(evaluate.load(), have)
        _save(PAIRS, pairs)
        print("pairs:", len(pairs))
        return
    if argv[:1] == ["run"]:
        from shop_comparator.matching.product_matcher import load_local_env
        load_local_env()
        run(argv[1], int(argv[2]) if len(argv) > 2 else None)
    print(render(report()))


if __name__ == "__main__":
    main(sys.argv[1:])

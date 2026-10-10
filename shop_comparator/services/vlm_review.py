"""VLM-assisted match review: the model suggests, a person decides.

Flow in the web app
  select products -> run a review job -> watch progress -> read each suggestion
  next to the images -> approve / reject / hold -> an approved match is what the
  price comparison uses.

Rules this module enforces
  * A suggestion is stored in `vlm_suggestions` and never changes a match.
    Only a person's decision (`match_reviews`) does.
  * The model's `confidence` is the model's own output, not a measured
    probability.  Nothing is approved because of it.
  * A verdict that names a product outside the candidate list, has the wrong
    shape, or was made for an older candidate list is kept as a record but is
    not offered as a suggestion.
  * `stored` mode replays verdicts saved earlier by vlm_match (no network call).
    `live` mode calls the paid API and needs an explicit call limit.
"""
import hashlib
import json
import os
import statistics
import time

from shop_comparator.core import app_logging as L
from shop_comparator.core.crawl_errors import CrawlError
from shop_comparator.matching import match_review, vlm_match
from shop_comparator.services import comparison_service as cmp
from shop_comparator.storage import store

DECISIONS = ("same", "different", "uncertain")
ACTIONS = ("approve", "reject", "hold")
STAGE = "vlm"
CONFIDENCE_NOTE = "모델이 스스로 출력한 값입니다. 검증된 정답 확률이 아니며 자동 승인에 쓰지 않습니다."


class InvalidAnswer(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def candidate_key(entry):
    """Fingerprint of the candidate list a verdict was made for."""
    ids = sorted(str(c["kreamId"]) for c in entry.get("candidates") or [])
    return hashlib.sha1(",".join(ids).encode("ascii")).hexdigest()[:12]


def image_problem(entry):
    """Why this product cannot be judged from images, or None."""
    if not entry.get("candidates"):
        return "no_candidates", "KREAM 검색 후보가 없습니다. 먼저 후보를 수집해 주세요."
    if not entry.get("musinsaImage") or not os.path.exists(os.path.join(match_review.ROOT, entry["musinsaImage"])):
        return "image_missing", "무신사 이미지가 저장되어 있지 않습니다."
    with_image = [c for c in entry["candidates"] if c.get("image") and os.path.exists(os.path.join(match_review.ROOT, c["image"]))]
    if not with_image:
        return "image_missing", "KREAM 후보 이미지가 저장되어 있지 않습니다."
    return None


def validate(entry, answer):
    """Normalise one model answer or raise InvalidAnswer.  Returns (decision, kream_id, confidence, reason)."""
    if not isinstance(answer, dict):
        raise InvalidAnswer("format", "모델 응답이 JSON 객체가 아닙니다.")
    decision = answer.get("decision")
    if decision not in DECISIONS:
        raise InvalidAnswer("format", "판정 값이 same / different / uncertain 중 하나가 아닙니다.")
    kream_id = str(answer.get("kream_id") or "").strip()
    known = {str(c["kreamId"]) for c in entry.get("candidates") or []}
    if decision == "same":
        if not kream_id:
            raise InvalidAnswer("format", "동일 판정인데 고른 후보가 없습니다.")
        if kream_id not in known:
            raise InvalidAnswer("unknown_candidate", f"모델이 후보 목록에 없는 상품({kream_id})을 골랐습니다.")
    else:
        kream_id = ""            # an id next to "different"/"uncertain" carries no meaning
    confidence = answer.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        confidence = None        # kept empty rather than guessed
    return decision, kream_id, confidence, str(answer.get("reason") or "")[:600]


def stored_verdicts():
    try:
        with open(vlm_match.RESULTS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _record(goods_no, entry, job_id, source, **values):
    return store.add_suggestion(goodsNo=goods_no, jobId=job_id, source=source, candidateKey=candidate_key(entry),
                                candidateIds=json.dumps(sorted(str(c["kreamId"]) for c in entry.get("candidates") or [])), **values)


def review_products(goods_nos, ctx, mode="stored", max_calls=None, force=False, judge=None):
    """Job body.  mode 'stored' replays saved verdicts; 'live' calls the model at most `max_calls` times."""
    if mode not in ("stored", "live"):
        raise ValueError("실행 방식은 stored 또는 live 입니다.")
    if mode == "live":
        if not isinstance(max_calls, int) or max_calls <= 0:
            raise ValueError("유료 호출은 최대 호출 수(1 이상)를 정해야 실행됩니다.")
        if judge is None and not os.getenv("OPENAI_API_KEY_VLM"):
            raise CrawlError("VLM", "prerequisite", ".env에 OPENAI_API_KEY_VLM이 없어 실행할 수 없습니다.")
    judge = judge or vlm_match.judge
    goods_nos = [str(g) for g in goods_nos]
    data, saved, calls = match_review.load(), stored_verdicts() if mode == "stored" else {}, 0
    latest = store.latest_suggestions(goods_nos, valid_only=True)
    store.plan_items(ctx.id, STAGE, goods_nos)
    ctx.note("mode", "저장된 판정 재생(네트워크 미호출)" if mode == "stored" else f"실시간 호출(최대 {max_calls}회)")
    with L.bind(stage="vlm-review"):
        for index, goods_no in enumerate(goods_nos):
            ctx.check()
            ctx.progress("VLM 검토", index, len(goods_nos))
            entry = data.get(goods_no) or {"candidates": []}
            with L.bind(goods=goods_no):
                problem = image_problem(entry)
                if problem:
                    _record(goods_no, entry, ctx.id, mode, status="error", errorKind=problem[0], error=problem[1])
                    store.mark_item(ctx.id, STAGE, goods_no, "failed", problem[0], problem[1])
                    ctx.count("failed")
                    L.warn(f"VLM 검토 불가: {problem[1]}")
                    continue
                key = candidate_key(entry)
                current = latest.get(goods_no)
                if current and current["candidateKey"] == key and not force:
                    store.mark_item(ctx.id, STAGE, goods_no, "skipped")
                    ctx.count("alreadyJudged")       # same candidates, verdict already stored: no duplicate run
                    continue
                started, usage, judged_at = time.time(), {}, store.now()
                try:
                    if mode == "stored":
                        verdict = saved.get(goods_no)
                        if not verdict or verdict.get("decision") not in DECISIONS:
                            raise InvalidAnswer("no_stored_verdict", "저장된 판정이 없습니다. 실시간 실행이 필요합니다.")
                        if (verdict.get("at") or "") < (entry.get("collectedAt") or ""):
                            raise InvalidAnswer("stale", "저장된 판정이 후보를 다시 수집하기 전의 것입니다.")
                        answer, usage, judged_at = verdict, verdict.get("usage") or {}, verdict.get("at") or judged_at
                        seconds = verdict.get("seconds")
                    else:
                        if calls >= max_calls:
                            ctx.count("notRunLimit")     # stays 'pending': a later run can pick it up
                            continue
                        calls += 1
                        answer, usage = judge(entry)
                        seconds = round(time.time() - started, 1)
                    decision, kream_id, confidence, reason = validate(entry, answer)
                except InvalidAnswer as exc:
                    _record(goods_no, entry, ctx.id, mode, status="invalid", errorKind=exc.kind, error=str(exc),
                            model=usage.get("model"), judgedAt=judged_at)
                    store.mark_item(ctx.id, STAGE, goods_no, "failed", exc.kind, str(exc))
                    ctx.count("failed")
                    L.warn(f"VLM 응답을 제안으로 쓰지 않습니다: {exc}")
                    continue
                except Exception as exc:                 # API failure, timeout: the stored matches are untouched
                    message = L.redact(str(exc))[:200]
                    _record(goods_no, entry, ctx.id, mode, status="error", errorKind="api", error=message)
                    store.mark_item(ctx.id, STAGE, goods_no, "failed", "api", message)
                    ctx.count("failed")
                    L.error(f"VLM 호출 실패: {type(exc).__name__}", "api")
                    continue
                _record(goods_no, entry, ctx.id, mode, status="valid", decision=decision, kreamId=kream_id, confidence=confidence,
                        reason=reason, model=usage.get("model"), inputTokens=usage.get("input_tokens"),
                        outputTokens=usage.get("output_tokens"), seconds=seconds, judgedAt=judged_at)
                store.mark_item(ctx.id, STAGE, goods_no, "ok")
                ctx.count("ok")
                ctx.count(decision)
        ctx.progress("VLM 검토", len(goods_nos), len(goods_nos))
        ctx.note("apiCalls", calls)


def _conflicts(entry, info, suggestion, rule):
    """Disagreements a reviewer should see before deciding."""
    out = []
    if not suggestion or suggestion["status"] != "valid":
        return out
    rule_id = str(rule["kreamId"]) if rule and rule["decision"] == "model_code" else ""
    picked = suggestion["kreamId"] if suggestion["decision"] == "same" else ""
    if rule_id and suggestion["decision"] == "same" and picked != rule_id:
        out.append(f"모델번호 규칙은 {rule_id}, VLM은 {picked}을(를) 골랐습니다.")
    if rule_id and suggestion["decision"] == "different":
        out.append(f"모델번호 규칙은 {rule_id}을(를) 같은 상품으로 보는데 VLM은 다른 상품이라고 판정했습니다.")
    if rule and rule["decision"] == "ambiguous" and picked:
        out.append("같은 모델번호를 쓰는 후보가 여러 개입니다. VLM이 그중 하나를 골랐습니다.")
    if info["usable"] and info["kreamId"]:
        if suggestion["decision"] == "same" and picked != info["kreamId"]:
            out.append(f"등록된 매칭은 {info['kreamId']}인데 VLM은 {picked}을(를) 골랐습니다.")
        elif suggestion["decision"] == "different":
            out.append(f"등록된 매칭({info['kreamId']}, {info['status']})을 VLM은 다른 상품이라고 판정했습니다.")
    if picked and not rule_id:
        chosen = next((c for c in entry["candidates"] if str(c["kreamId"]) == picked), {})
        codes = [c for c in match_review.musinsa_codes(entry) if match_review.usable_code(match_review.norm_code(c))]
        if codes and match_review.usable_code(match_review.norm_code(chosen.get("model"))) \
                and not any(match_review.code_match(code, chosen.get("model")) for code in codes):
            out.append(f"VLM이 고른 후보의 모델번호({chosen.get('model')})가 무신사 품번과 다릅니다.")
    return out


def queue(goods_nos):
    """Everything the review screen shows, per product."""
    goods_nos = [str(g) for g in goods_nos]
    data, matches = match_review.load(), store.all_matches()
    products = {str(p["goodsNo"]): p for p in store.products(active_only=False, goods_nos=goods_nos)}
    suggestions, valid = store.latest_suggestions(goods_nos), store.latest_suggestions(goods_nos, valid_only=True)
    rules = {d["goodsNo"]: d for d in match_review.decide(goods_nos)}
    history = {}
    for review in store.reviews(goods_nos):
        history.setdefault(review["goodsNo"], []).append(review)
    items = []
    for goods_no in goods_nos:
        product = products.get(goods_no) or {}
        entry = {**(data.get(goods_no) or {"candidates": []}), "goodsNo": goods_no,
                 "brand": product.get("brandName"), "name": product.get("goodsName"), "serial": product.get("serial_number")}
        info = cmp.match_info(matches.get(goods_no))
        suggestion, attempt = valid.get(goods_no), suggestions.get(goods_no)
        stale = bool(suggestion and suggestion["candidateKey"] != candidate_key(entry))
        usable = None if stale else suggestion
        reviews = history.get(goods_no, [])
        last = reviews[-1] if reviews else None
        if last and last["action"] in ("approve", "reject"):
            state = "approved" if last["action"] == "approve" else "rejected"
        elif last:
            state = "held"
        elif usable:
            state = "pending"
        else:
            state = "no_suggestion"
        items.append({
            "goodsNo": goods_no, "brand": entry["brand"] or "", "name": entry["name"] or "", "serial": entry["serial"] or "",
            "musinsaUrl": f"https://www.musinsa.com/products/{goods_no}",
            "musinsaImage": f"/api/review/image/{goods_no}/musinsa.jpg" if entry.get("musinsaImage") else None,
            "candidates": [{"kreamId": str(c["kreamId"]), "brand": c.get("brand") or "", "name": c.get("name") or "", "model": c.get("model") or "",
                            "url": f"https://kream.co.kr/products/{c['kreamId']}",
                            "image": f"/api/review/image/{goods_no}/kream_{c['kreamId']}.jpg" if c.get("image") else None}
                           for c in entry["candidates"]],
            "match": info, "state": state,
            "suggestion": None if not usable else {
                "id": usable["id"], "decision": usable["decision"], "kreamId": usable["kreamId"], "confidence": usable["confidence"],
                "reason": usable["reason"], "model": usable["model"], "judgedAt": usable["judgedAt"], "source": usable["source"]},
            "stale": stale,
            "lastAttempt": None if not attempt or attempt["status"] == "valid" else {
                "status": attempt["status"], "errorKind": attempt["errorKind"], "error": attempt["error"], "at": attempt["createdAt"]},
            "conflicts": _conflicts(entry, info, usable, rules.get(goods_no)),
            "rule": (rules.get(goods_no) or {}).get("decision") or "no_candidates",
            "review": last and {"action": last["action"], "kreamId": last["kreamId"], "note": last["note"], "at": last["createdAt"],
                                "seconds": last["reviewSeconds"], "mode": last["mode"]},
            "reviewCount": len(reviews),
        })
    return items


def decide(goods_no, action, kream_id=None, note=None, seconds=None, mode="assisted"):
    """Record a person's decision and, for approve / reject, apply it to the match used for prices."""
    goods_no = str(goods_no)
    if action not in ACTIONS:
        raise ValueError("승인, 거절, 보류 중 하나를 선택해 주세요.")
    if mode not in ("assisted", "manual"):
        raise ValueError("검토 방식이 올바르지 않습니다.")
    if not store.product(goods_no):
        raise ValueError("목록에 없는 무신사 상품입니다.")
    entry = match_review.load().get(goods_no) or {"candidates": []}
    known = {str(c["kreamId"]) for c in entry["candidates"]}
    previous = cmp.match_info(store.match_for(goods_no))
    suggestion = store.latest_suggestions([goods_no], valid_only=True).get(goods_no)
    if suggestion and suggestion["candidateKey"] != candidate_key(entry):
        suggestion = None
    kream_id = str(kream_id or "").strip()
    if action == "approve":
        if not kream_id.isdigit():
            raise ValueError("승인할 KREAM 후보를 선택해 주세요.")
        if kream_id not in known and kream_id != previous["kreamId"]:
            raise ValueError("후보 목록에 없는 KREAM 상품은 승인할 수 없습니다. 매칭 탭에서 직접 지정해 주세요.")
    if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 <= seconds <= 3600):
        seconds = None                       # an implausible timer value is dropped, not stored
    review_id = store.add_review(goods_no, action, kream_id or None, note, suggestion, previous, mode, seconds)
    if action == "approve":
        agreed = suggestion and suggestion["decision"] == "same" and suggestion["kreamId"] == kream_id
        basis = f"VLM 제안({suggestion['model'] or 'VLM'})과 일치" if agreed else "VLM 제안과 무관하게 사람이 선택" if suggestion else "VLM 제안 없이 사람이 선택"
        store.set_manual_match(goods_no, kream_id, f"사람 검수 승인 #{review_id}: {basis}", method="human_review", confidence=None)
    elif action == "reject" and previous["kreamId"] and (not kream_id or kream_id == previous["kreamId"]):
        store.clear_match(goods_no)          # the rejected product must not feed the price comparison
    L.log(f"매칭 검수: {action} (KREAM {kream_id or '-'})", goods=goods_no, stage="vlm-review")
    return review_id


def _median(values):
    return round(statistics.median(values), 1) if values else None


def metrics(goods_nos):
    """Counts for the review workload.  Review time only comes from decisions people made in the UI."""
    goods_nos = [str(g) for g in goods_nos]
    items = queue(goods_nos)
    methods = [i["match"]["method"] if i["match"]["usable"] else "" for i in items]
    reviews = store.reviews(goods_nos)
    last = {r["goodsNo"]: r for r in reviews}
    timed = {mode: [r["reviewSeconds"] for r in reviews if r["mode"] == mode and r["reviewSeconds"] is not None]
             for mode in ("assisted", "manual")}
    suggestions = [i["suggestion"] for i in items if i["suggestion"]]
    seconds = [s["seconds"] for s in store.latest_suggestions(goods_nos, valid_only=True).values() if s.get("seconds") is not None]
    tokens = store.latest_suggestions(goods_nos, valid_only=True).values()
    return {
        "products": len(items),
        "ruleConfirmed": sum(1 for m in methods if m == "model_code"),
        "humanApprovedMatches": sum(1 for m in methods if m == "human_review"),
        "otherMatches": sum(1 for m in methods if m and m not in ("model_code", "human_review")),
        "unmatched": sum(1 for m in methods if not m),
        "vlmTargets": sum(1 for i in items if not (i["match"]["usable"] and i["match"]["method"] == "model_code")),
        "vlmJudged": len(suggestions),
        "vlmSame": sum(1 for s in suggestions if s["decision"] == "same"),
        "vlmDifferent": sum(1 for s in suggestions if s["decision"] == "different"),
        "vlmUncertain": sum(1 for s in suggestions if s["decision"] == "uncertain"),
        "vlmFailed": sum(1 for i in items if i["lastAttempt"]),
        "stale": sum(1 for i in items if i["stale"]),
        "needsReview": sum(1 for i in items if i["state"] == "pending"),
        "approved": sum(1 for r in last.values() if r["action"] == "approve"),
        "rejected": sum(1 for r in last.values() if r["action"] == "reject"),
        "held": sum(1 for r in last.values() if r["action"] == "hold"),
        "approvedWithVlm": sum(1 for r in last.values() if r["action"] == "approve" and r["suggestionDecision"] == "same"
                               and r["suggestionKreamId"] == r["kreamId"]),
        "decisions": len(reviews),
        "vlmSecondsMedian": _median(seconds),
        "inputTokens": sum(s.get("inputTokens") or 0 for s in tokens), "outputTokens": sum(s.get("outputTokens") or 0 for s in tokens),
        "reviewSeconds": {mode: {"count": len(values), "median": _median(values)} for mode, values in timed.items()},
        "confidenceNote": CONFIDENCE_NOTE,
    }

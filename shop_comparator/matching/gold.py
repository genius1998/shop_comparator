"""Evaluation ground truth made by a person, kept apart from the working database.

Why a separate store
  The labels are evidence.  They must not change when the shop data changes, and
  building them must not touch the working database.  Everything lives under
  evaluation_data/gold/ (not in git):

    cases.json            frozen inputs of each case: product, candidates, options, image files, hashes
    method_outputs.json   what each matching method answers for the same inputs (never sent to the labelling screen)
    images/<case>/...     copies of the thumbnails as they were when the case was frozen
    labels.sqlite         labels, with reviewer, time, origin; and review timing records

  `publish` copies the two JSON files (text only) to docs/evaluation/gold/ so they can be committed.

What the labelling screen may show
  Product and candidates: image, name, brand, model code, options.  Nothing else.
  No model verdict, no confidence, no suggested candidate, no rule decision, no current match.
  Candidates are shown in a fixed shuffled order so position does not give the search rank away.

Who made a label
  origin = human_ui     a person used the labelling screen of the normal server
           automation   a script drove the screen (demo and capture runs set SHOP_AUTOMATION=1)
  Only human_ui labels count as ground truth.  Nothing in this module creates a label by itself.

    python -m shop_comparator.matching.gold build      # freeze 60 cases from match_review/ (reads the working DB, never writes it)
    python -m shop_comparator.matching.gold status
    python -m shop_comparator.matching.gold publish
    python -m shop_comparator.matching.gold export     # labels and timing as CSV
"""
import csv
import hashlib
import io
import json
import os
import random
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

from shop_comparator.core.utils import get_persistent_path

GOLD_DIR = get_persistent_path(os.path.join("evaluation_data", "gold"))
PUBLISHED = os.path.join("docs", "evaluation", "gold")
SEED = 20261010
TARGET = 60
DECISIONS = ("candidate", "none", "unsure")
ORIGINS = ("human_ui", "automation")
# Selection groups.  They only make the sample varied; they are never used as an answer.
STRATA = {
    "code_single": "모델번호가 일치하는 후보가 하나인 상품",
    "conflict": "색상·모델 충돌이 의심되는 상품 (같은 모델번호 후보 여러 개, 규칙과 VLM 불일치, 같은 계열의 다른 모델번호 후보)",
    "similar": "모델번호로 확정하지 못했고 닮은 후보가 있는 상품 (규칙 local 등록, 모델 등록, VLM 동일 제안)",
    "maybe_none": "같은 상품이 없을 가능성이 있는 상품 (VLM 다름·불확실)",
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def path(*parts):
    return os.path.join(GOLD_DIR, *parts)


def _sha(file_path):
    with open(file_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _load(name, default):
    try:
        with open(path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def cases():
    return _load("cases.json", {"cases": []})


def method_outputs():
    return _load("method_outputs.json", {})


# ------------------------------------------------------------------ labels store
def _db():
    os.makedirs(GOLD_DIR, exist_ok=True)
    conn = sqlite3.connect(path("labels.sqlite"))
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS gold_labels (
        id INTEGER PRIMARY KEY AUTOINCREMENT, caseId TEXT NOT NULL, reviewer TEXT NOT NULL, origin TEXT NOT NULL,
        decision TEXT NOT NULL, kreamId TEXT, rationale TEXT, snapshotHash TEXT,
        startedAt TEXT, endedAt TEXT, seconds REAL, hiddenSeconds REAL, interruptions INTEGER, createdAt TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS review_timing (
        id INTEGER PRIMARY KEY AUTOINCREMENT, context TEXT NOT NULL, caseId TEXT NOT NULL, mode TEXT NOT NULL, origin TEXT NOT NULL,
        reviewer TEXT, decision TEXT, kreamId TEXT, startedAt TEXT, endedAt TEXT, seconds REAL, hiddenSeconds REAL,
        interruptions INTEGER, reviewId INTEGER, createdAt TEXT)""")
    return conn


def origin_now():
    """Labels made while a script drives the app are marked as such, whatever the request says."""
    return "automation" if os.getenv("SHOP_AUTOMATION") == "1" else "human_ui"


def _clock(started, ended, seconds, hidden, interruptions):
    def number(value, top):
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= top else None
    return (str(started)[:40] if started else None, str(ended)[:40] if ended else None, number(seconds, 7200),
            number(hidden, 7200), int(interruptions) if isinstance(interruptions, int) and 0 <= interruptions <= 10000 else None)


def add_label(case_id, reviewer, decision, kream_id=None, rationale=None, started=None, ended=None, seconds=None, hidden=None,
              interruptions=None):
    """Store one label.  Returns its id.  Earlier labels of the same case are kept; the newest one counts."""
    case = next((c for c in cases()["cases"] if c["caseId"] == case_id), None)
    if not case:
        raise ValueError("평가 사례를 찾을 수 없습니다.")
    reviewer = " ".join(str(reviewer or "").split())[:40]
    if len(reviewer) < 2:
        raise ValueError("검수자 이름(별칭)을 2자 이상 입력해 주세요.")
    if decision not in DECISIONS:
        raise ValueError("후보 선택, 동일 상품 없음, 판단 불가 중 하나를 골라 주세요.")
    kream_id = str(kream_id or "").strip()
    rationale = " ".join(str(rationale or "").split())[:500]
    if decision == "candidate":
        if kream_id not in {c["kreamId"] for c in case["candidates"]}:
            raise ValueError("이 사례의 후보 중에서 선택해 주세요.")
    else:
        kream_id = ""
        if len(rationale) < 2:
            raise ValueError("동일 상품 없음·판단 불가는 판단 근거를 적어 주세요.")
    started, ended, seconds, hidden, interruptions = _clock(started, ended, seconds, hidden, interruptions)
    origin = origin_now()
    with _db() as conn:
        cursor = conn.execute(
            """INSERT INTO gold_labels (caseId, reviewer, origin, decision, kreamId, rationale, snapshotHash, startedAt, endedAt,
                                        seconds, hiddenSeconds, interruptions, createdAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (case_id, reviewer, origin, decision, kream_id or None, rationale or None, case["hash"], started, ended, seconds, hidden,
             interruptions, now()))
        conn.execute("""INSERT INTO review_timing (context, caseId, mode, origin, reviewer, decision, kreamId, startedAt, endedAt, seconds,
                                                   hiddenSeconds, interruptions, reviewId, createdAt)
                        VALUES ('gold', ?, 'blind', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (case_id, origin, reviewer, decision, kream_id or None, started, ended, seconds, hidden, interruptions, cursor.lastrowid, now()))
        return cursor.lastrowid


def add_timing(context, case_id, mode, decision, kream_id=None, reviewer=None, started=None, ended=None, seconds=None, hidden=None,
               interruptions=None, review_id=None):
    """Timing of an operational review decision (context 'ops').  Kept here so the working database is not changed."""
    started, ended, seconds, hidden, interruptions = _clock(started, ended, seconds, hidden, interruptions)
    with _db() as conn:
        conn.execute("""INSERT INTO review_timing (context, caseId, mode, origin, reviewer, decision, kreamId, startedAt, endedAt, seconds,
                                                   hiddenSeconds, interruptions, reviewId, createdAt)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (context, str(case_id), mode, origin_now(), " ".join(str(reviewer or "").split())[:40] or None, decision,
                      str(kream_id or "") or None, started, ended, seconds, hidden, interruptions, review_id, now()))


def labels(origin=None):
    with _db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM gold_labels ORDER BY id")]
    return [r for r in rows if origin is None or r["origin"] == origin]


def latest_labels(origin="human_ui", reviewer=None):
    """{caseId: newest label} of one origin (and one reviewer, when given)."""
    out = {}
    for row in labels(origin):
        if reviewer is None or row["reviewer"] == reviewer:
            out[row["caseId"]] = row
    return out


def timings():
    with _db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM review_timing ORDER BY id")]


def status():
    all_cases = cases()["cases"]
    human, automated = latest_labels("human_ui"), latest_labels("automation")
    by = {}
    for label in human.values():
        by[label["decision"]] = by.get(label["decision"], 0) + 1
    return {"cases": len(all_cases), "humanLabelled": len(human), "humanRemaining": len(all_cases) - len(human),
            "humanByDecision": by, "automationRecords": len(automated),
            "reviewers": sorted({l["reviewer"] for l in human.values()}),
            "note": "사람이 정답 검수 화면에서 저장한 라벨만 정답으로 센다. 자동화·AI가 만든 기록은 별도로 센다."}


# ------------------------------------------------------------------ what the labelling screen gets
def public_case(case):
    """The case as the reviewer may see it: no method output, no group, no search order."""
    # KREAM sizes were only collected for products that had been matched.  Showing them for some
    # candidates and not for others would point at the earlier match, so they are shown for all or none.
    complete = all(c.get("options") for c in case["candidates"])
    return {"caseId": case["caseId"], "musinsa": case["musinsa"], "candidateOptionsShown": complete,
            "candidates": [{**{k: c[k] for k in ("kreamId", "name", "brand", "model", "image", "url")},
                            "options": c["options"] if complete else None} for c in case["candidates"]]}


def queue(reviewer=None):
    """Cases with only the reviewer's own progress attached (their newest label, nothing from any method)."""
    mine = latest_labels(origin_now(), reviewer) if reviewer else {}
    out = []
    for case in cases()["cases"]:
        item = public_case(case)
        label = mine.get(case["caseId"])
        item["label"] = label and {k: label[k] for k in ("decision", "kreamId", "rationale", "createdAt", "seconds")}
        out.append(item)
    return out


# ------------------------------------------------------------------ building the frozen sample
def _options(conn, sql, key):
    return [r[0] for r in conn.execute(sql, (key,))]


def build(source_root=None, size=TARGET, db_file=None):
    """Freeze `size` cases.  Reads match_review/ and the working database (read only); writes only under evaluation_data/gold/."""
    from shop_comparator.matching import evaluate, match_review, method_eval
    from shop_comparator.services import comparison_service as cmp

    root = source_root or get_persistent_path(".")
    review_dir = os.path.join(root, "match_review")
    with open(os.path.join(review_dir, "candidates.json"), encoding="utf-8") as f:
        data = json.load(f)
    try:
        with open(os.path.join(review_dir, "vlm_results.json"), encoding="utf-8") as f:
            verdicts = json.load(f)
    except (OSError, ValueError):
        verdicts = {}
    db = sqlite3.connect(f"file:{db_file or os.path.join(root, 'musinsa_data.sqlite')}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    matches = {r["musinsa_goods_no"]: dict(r) for r in db.execute("SELECT * FROM kream_search_cache")}
    products = {str(r["goodsNo"]): dict(r) for r in db.execute("SELECT * FROM products")}

    def has_images(goods_no, entry):
        files = [entry.get("musinsaImage")] + [c.get("image") for c in entry["candidates"]]
        return all(f and os.path.exists(os.path.join(review_dir, f)) for f in files)

    pool = {g: e for g, e in data.items() if g in products and e.get("candidates") and has_images(g, e)}
    info = {}
    for goods_no, entry in pool.items():
        eval_entry = {"goodsNo": goods_no, "brand": entry.get("brand"), "name": entry.get("name"), "serial": entry.get("serial"),
                      "candidates": [{"kreamId": str(c["kreamId"]), "name": c.get("name"), "model": c.get("model") or ""} for c in entry["candidates"]]}
        picked, decision = evaluate.model_code_pick(eval_entry)
        verdict = verdicts.get(goods_no) or {}
        fresh = verdict.get("decision") in ("same", "different", "uncertain") and (verdict.get("at") or "") >= (entry.get("collectedAt") or "")
        registered = cmp.match_info(matches.get(goods_no))
        stems = [method_eval.family(eval_entry, c["kreamId"]) for c in eval_entry["candidates"] if c["model"]]
        info[goods_no] = {"entry": eval_entry, "rule": decision, "rulePick": picked, "vlm": verdict if fresh else None,
                          "registered": registered, "variants": len(stems) != len(set(stems))}
    rng = random.Random(SEED)

    def take(candidates, count, chosen):
        candidates = [g for g in candidates if g not in chosen]
        rng.shuffle(candidates)
        picked = candidates[:count]
        chosen.update(picked)
        return picked

    chosen, groups = set(), {}
    conflict_first = [g for g, i in info.items() if i["rule"] == "ambiguous"
                      or (i["rule"] == "model_code" and i["vlm"] and (i["vlm"]["decision"] != "same" or str(i["vlm"].get("kream_id")) != i["rulePick"]))]
    groups["conflict"] = take(conflict_first, 15, chosen)
    groups["conflict"] += take([g for g, i in info.items() if i["rule"] == "model_code" and i["variants"]], 15 - len(groups["conflict"]), chosen)
    similar_first = [g for g, i in info.items() if i["rule"] != "model_code" and i["registered"]["method"] == "local"]
    groups["similar"] = take(similar_first, 15, chosen)
    groups["similar"] += take([g for g, i in info.items() if i["rule"] != "model_code" and i["registered"]["kind"] == "ai_unreviewed"], 7, chosen)
    groups["similar"] += take([g for g, i in info.items() if i["rule"] != "model_code" and i["vlm"] and i["vlm"]["decision"] == "same"],
                              15 - len(groups["similar"]), chosen)
    groups["maybe_none"] = take([g for g, i in info.items() if i["rule"] != "model_code" and i["vlm"] and i["vlm"]["decision"] == "uncertain"], 3, chosen)
    groups["maybe_none"] += take([g for g, i in info.items() if i["rule"] != "model_code" and i["vlm"] and i["vlm"]["decision"] == "different"],
                                 15 - len(groups["maybe_none"]), chosen)
    groups["code_single"] = take([g for g, i in info.items() if i["rule"] == "model_code" and len(i["entry"]["candidates"]) > 1],
                                 size - sum(len(v) for v in groups.values()), chosen)

    if os.path.isdir(path("images")):
        shutil.rmtree(path("images"))
    ordered = [(stratum, g) for stratum in ("code_single", "conflict", "similar", "maybe_none") for g in groups[stratum]]
    rng.shuffle(ordered)                      # the case number must not reveal the group
    out_cases, outputs = [], {}
    for number, (stratum, goods_no) in enumerate(ordered, 1):
        case_id = f"G{number:03d}"
        entry, meta, product = data[goods_no], info[goods_no], products[goods_no]
        folder = path("images", case_id)
        os.makedirs(folder)
        shutil.copyfile(os.path.join(review_dir, entry["musinsaImage"]), os.path.join(folder, "musinsa.jpg"))
        hashes = {"musinsa.jpg": _sha(os.path.join(folder, "musinsa.jpg"))}
        candidates = []
        for c in entry["candidates"]:
            kream_id = str(c["kreamId"])
            name = f"kream_{kream_id}.jpg"
            shutil.copyfile(os.path.join(review_dir, c["image"]), os.path.join(folder, name))
            hashes[name] = _sha(os.path.join(folder, name))
            sizes = _options(db, "SELECT size FROM kream_quotes WHERE kreamId = ? AND COALESCE(present, 1) = 1 ORDER BY size", kream_id)
            candidates.append({"kreamId": kream_id, "name": c.get("name") or "", "brand": c.get("brand") or "", "model": c.get("model") or "",
                               "options": sizes or None, "image": f"{case_id}/{name}", "url": f"https://kream.co.kr/products/{kream_id}"})
        search_order = [c["kreamId"] for c in candidates]
        rng.shuffle(candidates)
        musinsa = {"goodsNo": goods_no, "brand": entry.get("brand") or product.get("brandName") or "",
                   "name": entry.get("name") or product.get("goodsName") or "", "model": entry.get("serial") or product.get("serial_number") or "",
                   "options": _options(db, "SELECT size FROM musinsa_variants WHERE goodsNo = ? AND COALESCE(present, 1) = 1 ORDER BY size", goods_no) or None,
                   "image": f"{case_id}/musinsa.jpg", "url": f"https://www.musinsa.com/products/{goods_no}"}
        frozen = {"caseId": case_id, "stratum": stratum, "musinsa": musinsa, "candidates": candidates, "imageHashes": hashes,
                  "source": {"collected": "2026-10-09 무신사 신발 랭킹 수집분 (match_review/candidates.json)",
                             "candidatesCollectedAt": entry.get("collectedAt"), "frozenAt": now()}}
        frozen["hash"] = hashlib.sha256(json.dumps({k: frozen[k] for k in ("musinsa", "candidates", "imageHashes")},
                                                   ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        out_cases.append(frozen)
        verdict = meta["vlm"]
        outputs[case_id] = {
            "goodsNo": goods_no, "searchOrder": search_order,
            "vlm": None if not verdict else {"decision": verdict["decision"], "kreamId": str(verdict.get("kream_id") or ""),
                                             "model": (verdict.get("usage") or {}).get("model"), "at": verdict.get("at"),
                                             "confidence": verdict.get("confidence")},
            "registered": {"kind": meta["registered"]["kind"], "method": meta["registered"]["method"], "kreamId": meta["registered"]["kreamId"]},
        }
    db.close()
    os.makedirs(GOLD_DIR, exist_ok=True)
    with open(path("cases.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"seed": SEED, "builtAt": now(), "strata": STRATA,
                   "strataCounts": {k: len(v) for k, v in groups.items()},
                   "selectionNote": "표본 선정 기준일 뿐이며 정답이 아니다. 기존 규칙·VLM의 결론을 기준으로 골랐으므로 무작위 표본이 아니다.",
                   "cases": out_cases}, f, ensure_ascii=False, indent=1)
    with open(path("method_outputs.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(outputs, f, ensure_ascii=False, indent=1)
    return {k: len(v) for k, v in groups.items()}


def publish():
    os.makedirs(PUBLISHED, exist_ok=True)
    for name in ("cases.json", "method_outputs.json"):
        shutil.copyfile(path(name), os.path.join(PUBLISHED, name))
    return PUBLISHED


def _csv(rows, fields):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(fields)
    for row in rows:
        writer.writerow(["'" + str(row.get(f)) if isinstance(row.get(f), str) and row[f][:1] in "=+-@" else row.get(f) for f in fields])
    return "﻿" + out.getvalue()


LABEL_FIELDS = ["id", "caseId", "reviewer", "origin", "decision", "kreamId", "rationale", "snapshotHash", "startedAt", "endedAt", "seconds",
                "hiddenSeconds", "interruptions", "createdAt"]
TIMING_FIELDS = ["id", "context", "caseId", "mode", "origin", "reviewer", "decision", "kreamId", "startedAt", "endedAt", "seconds",
                 "hiddenSeconds", "interruptions", "reviewId", "createdAt"]


def labels_csv():
    return _csv(labels(), LABEL_FIELDS)


def timing_csv():
    return _csv(timings(), TIMING_FIELDS)


def main(argv):
    command = argv[0] if argv else "status"
    if command == "build":
        if labels("human_ui") and "--force" not in argv:
            sys.exit("이미 사람 라벨이 있습니다. 사례를 다시 만들면 라벨과 어긋날 수 있어 중단합니다(--force로 강행).")
        print("frozen:", build())
    elif command == "publish":
        print("published to", publish())
    elif command == "export":
        for name, text in (("gold_labels.csv", labels_csv()), ("review_timing.csv", timing_csv())):
            with open(path(name), "w", encoding="utf-8", newline="") as f:
                f.write(text)
            print(path(name))
    print(json.dumps(status(), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])

"""Vision-model check of saved match-review images (opt-in).

Sends the locally saved Musinsa thumbnail and KREAM candidate thumbnails
(match_review/images/...) to a vision model and asks whether one of the
candidates is the same product.  Two backends share one prompt and one
answer schema, so their results can be compared:

  openai   OpenAI Responses API (paid).  Token usage of every call is recorded.
  ollama   a model served by a local Ollama (free, runs on this PC).

    python -m shop_comparator.matching.vlm_match 10            # first 10 products of the last batch
    python -m shop_comparator.matching.vlm_match 123 456       # specific Musinsa product numbers
    python -m shop_comparator.matching.vlm_match pending       # only products the model-code check left open
    python -m shop_comparator.matching.vlm_match --local all   # every stored product, local model

OpenAI: key OPENAI_API_KEY_VLM (.env), model OPENAI_VLM_MODEL, else OPENAI_MATCH_MODEL.
Ollama: model OLLAMA_VLM_MODEL (default qwen3-vl:8b-instruct-q4_K_M), server OLLAMA_HOST.
The key is never logged or written anywhere.  Results go to
match_review/vlm_results.json (OpenAI) or vlm_results_local.json (Ollama);
nothing is registered as a match here.
"""
import base64
import json
import os
import sys
import time

import requests

from shop_comparator.matching import match_review
from shop_comparator.storage import store

RESULTS = os.path.join(match_review.ROOT, "vlm_results.json")
LOCAL_RESULTS = os.path.join(match_review.ROOT, "vlm_results_local.json")
LOCAL_MODEL = "qwen3-vl:8b-instruct-q4_K_M"
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": ["same", "different", "uncertain"]},
        "kream_id": {"type": "string"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["decision", "kream_id", "confidence", "reason"],
}
SYSTEM = ("You verify whether two marketplace listings show the same product. Be conservative. "
          "Different colorway, collaboration edition, men's/women's variant or model generation means different. "
          "If the images cannot settle it, answer uncertain. Answer in Korean.")


def _b64(path):
    with open(os.path.join(match_review.ROOT, path), "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def _image(path):
    return "data:image/jpeg;base64," + _b64(path)


def model_name():
    return os.getenv("OPENAI_VLM_MODEL") or os.getenv("OPENAI_MATCH_MODEL") or "gpt-5.4-nano"


def local_model_name():
    return os.getenv("OLLAMA_VLM_MODEL") or LOCAL_MODEL


def question(entry):
    """The facts given to the model, identical for every backend."""
    candidates = [c for c in entry["candidates"] if c.get("image")]
    text = {"task": "Is one of the KREAM candidates the same product as the Musinsa product? "
                    "kream_id must be the matching candidate id, or an empty string.",
            "musinsa": {"brand": entry.get("brand"), "name": entry.get("name"), "model_code": entry.get("serial")},
            "candidates": [{"kream_id": c["kreamId"], "brand": c.get("brand"), "name": c.get("name"),
                            "model_code": c.get("model") or None} for c in candidates]}
    return text, candidates


def judge(entry, timeout=60):
    """One OpenAI call for one Musinsa product. Returns (result, usage)."""
    key = os.getenv("OPENAI_API_KEY_VLM")
    if not key:
        raise RuntimeError(".env에 OPENAI_API_KEY_VLM이 없습니다.")
    text, candidates = question(entry)
    content = [{"type": "input_text", "text": json.dumps(text, ensure_ascii=False)}]
    if entry.get("musinsaImage"):
        content += [{"type": "input_text", "text": "Musinsa image"},
                    {"type": "input_image", "image_url": _image(entry["musinsaImage"]), "detail": "low"}]
    for c in candidates:
        content += [{"type": "input_text", "text": f"KREAM candidate {c['kreamId']}"},
                    {"type": "input_image", "image_url": _image(c["image"]), "detail": "low"}]
    payload = {"model": model_name(),
               "input": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}],
               "text": {"format": {"type": "json_schema", "name": "image_match", "strict": True, "schema": SCHEMA}},
               "reasoning": {"effort": "low"}, "max_output_tokens": 600, "store": False}
    response = requests.post("https://api.openai.com/v1/responses", json=payload, timeout=timeout,
                             headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    if response.status_code != 200:
        try:
            message = (response.json().get("error") or {}).get("message", "")[:200]
        except ValueError:
            message = ""
        raise RuntimeError(f"OpenAI API {response.status_code}: {message}")
    body = response.json()
    text_out = ""
    for item in body.get("output", []):
        for part in item.get("content", []) or []:
            if part.get("type") == "output_text":
                text_out += part.get("text", "")
    usage = body.get("usage") or {}
    return json.loads(text_out), {
        "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
        "cached_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens"),
        "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
        "images": 1 + len(candidates), "model": body.get("model")}


def judge_local(entry, timeout=1800):
    """One call to a local Ollama vision model. Returns (result, usage).

    Ollama takes the images as one list, so the text says which image is which.
    """
    text, candidates = question(entry)
    images, order = [], []
    if entry.get("musinsaImage"):
        images.append(_b64(entry["musinsaImage"]))
        order.append("Musinsa product")
    for c in candidates:
        images.append(_b64(c["image"]))
        order.append(f"KREAM candidate {c['kreamId']}")
    text["image_order"] = order
    host = (os.getenv("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
    if not host.startswith("http"):
        host = "http://" + host
    payload = {"model": local_model_name(), "stream": False, "format": SCHEMA, "options": {"temperature": 0, "num_predict": 400},
               "messages": [{"role": "system", "content": SYSTEM},
                            {"role": "user", "content": json.dumps(text, ensure_ascii=False), "images": images}]}
    try:
        response = requests.post(host + "/api/chat", json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(f"Ollama에 연결하지 못했습니다({host}): {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise RuntimeError(f"Ollama {response.status_code}: {response.text[:200]}")
    body = response.json()
    result = json.loads((body.get("message") or {}).get("content") or "{}")
    if result.get("decision") not in ("same", "different", "uncertain"):
        raise RuntimeError("모델 응답이 정해진 형식이 아닙니다.")
    result = {"decision": result["decision"], "kream_id": str(result.get("kream_id") or ""),
              "confidence": result.get("confidence") or 0, "reason": str(result.get("reason") or "")}
    return result, {"input_tokens": body.get("prompt_eval_count"), "output_tokens": body.get("eval_count"),
                    "images": len(images), "model": body.get("model") or local_model_name()}


def run(goods_nos, local=False, skip_done=False):
    data = match_review.load()
    path, ask = (LOCAL_RESULTS, judge_local) if local else (RESULTS, judge)
    try:
        with open(path, encoding="utf-8") as f:
            results = json.load(f)
    except (OSError, ValueError):
        results = {}
    for goods_no in goods_nos:
        entry = data.get(str(goods_no))
        if not entry or not entry.get("candidates"):
            continue
        if skip_done and (results.get(str(goods_no)) or {}).get("decision") in ("same", "different", "uncertain"):
            continue
        started = time.time()
        try:
            result, usage = ask(entry)
            results[str(goods_no)] = {**result, "usage": usage, "seconds": round(time.time() - started, 1), "at": store.now()}
        except Exception as exc:
            results[str(goods_no)] = {"decision": "error", "kream_id": "", "confidence": 0, "reason": str(exc)[:200], "usage": {},
                                      "seconds": round(time.time() - started, 1)}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)
        r = results[str(goods_no)]
        print(goods_no, r["decision"], r["kream_id"], r["confidence"], f"{r.get('seconds')}s", r.get("usage"), r["reason"][:60], flush=True)
    return results


def main(argv):
    from shop_comparator.matching.product_matcher import load_local_env
    load_local_env()
    store.init()
    local = "--local" in argv
    args = [a for a in argv if a != "--local"] or ["10"]
    batch = (store.get_setting("last_collection") or {}).get("goods") or []
    if args == ["pending"]:  # only products the model-code check could not settle
        targets = [row["goodsNo"] for row in match_review.decide() if row["decision"] != "model_code"]
    elif args == ["all"]:
        targets = list(match_review.load())
    else:
        targets = batch[:int(args[0])] if len(args) == 1 and len(args[0]) <= 3 else args
    run(targets, local=local, skip_done=args == ["all"])


if __name__ == "__main__":
    main(sys.argv[1:])

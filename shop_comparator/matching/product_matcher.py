# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import requests


DEFAULT_OPENAI_MATCH_MODEL = "gpt-5.4-nano"
DEFAULT_OPENAI_MATCH_MAX_OUTPUT_TOKENS = "500"
DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_OPENAI_EMBEDDING_DIMENSIONS = "256"
_ENV_LOADED = False


def load_local_env(env_path: str | Path | None = None, override: bool = True) -> None:
    """Load local .env values, preferring project-local settings by default."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return

    path = Path(env_path) if env_path else Path(__file__).resolve().parents[2] / ".env"
    if not path.exists():
        _ENV_LOADED = True
        return

    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        _ENV_LOADED = True
        return

    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        if raw.startswith("export "):
            raw = raw[len("export ") :].strip()
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            if override:
                os.environ[key] = value
            else:
                os.environ.setdefault(key, value)

    _ENV_LOADED = True


BRAND_ALIASES = {
    "아디다스": "adidas",
    "나이키": "nike",
    "아식스": "asics",
    "살로몬": "salomon",
    "휠라": "fila",
    "뉴발란스": "new balance",
    "반스": "vans",
    "컨버스": "converse",
    "푸마": "puma",
    "리복": "reebok",
    "오찌": "otz",
    "아카이브앱크": "archivepke",
}

GENERIC_TOKENS = {
    "og",
    "m",
    "w",
    "men",
    "women",
    "mens",
    "womens",
    "shoe",
    "shoes",
    "sneaker",
    "sneakers",
    "black",
    "white",
    "화이트",
    "블랙",
}


@dataclass
class MatchDecision:
    matched: bool
    product: dict[str, Any] | None
    confidence: float
    method: str
    reason: str


def normalize_brand(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    raw = re.sub(r"\([^)]*\)", "", raw).strip()
    return BRAND_ALIASES.get(raw, raw).replace("&", "and")


def normalize_text(value: str | None) -> str:
    text = (value or "").lower()
    text = re.sub(r"[\[\]{}()]", " ", text)
    text = re.sub(r"[_:/\\|,+]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def alnum_compact(value: str | None) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", value or "").lower()


def extract_style_codes(*values: str | None) -> list[str]:
    patterns = [
        r"[A-Z]{1,5}\d{3,}[A-Z0-9]*(?:[-_][A-Z0-9]{2,})*",
        r"\d{3,}[A-Z]{1,5}(?:[-_][A-Z0-9]{2,})*",
        r"[A-Z0-9]{2,}[-_][A-Z0-9]{2,}(?:[-_][A-Z0-9]{2,})*",
    ]
    out: list[str] = []
    seen = set()
    for value in values:
        text = (value or "").upper()
        for pattern in patterns:
            for match in re.findall(pattern, text):
                code = match.strip("-_")
                if len(code) < 5 or code in seen or not any(ch.isdigit() for ch in code):
                    continue
                seen.add(code)
                out.append(code)
    return out


def token_set(value: str | None) -> set[str]:
    return {
        token
        for token in re.split(r"[^0-9a-zA-Z가-힣]+", normalize_text(value))
        if len(token) >= 2 and token not in GENERIC_TOKENS
    }


def token_overlap(left: str | None, right: str | None) -> float:
    a = token_set(left)
    b = token_set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a), len(b))


def name_similarity(left: str | None, right: str | None) -> float:
    a = normalize_text(left)
    b = normalize_text(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def candidate_text(candidate: dict[str, Any]) -> str:
    return " ".join(
        str(candidate.get(key) or "")
        for key in ("brand", "name_ko", "all_text", "product_url", "id")
    )


def score_candidate(musinsa: dict[str, Any], candidate: dict[str, Any]) -> tuple[float, str]:
    musinsa_brand = normalize_brand(musinsa.get("brand"))
    candidate_brand = normalize_brand(candidate.get("brand"))
    brand_match = bool(
        musinsa_brand
        and candidate_brand
        and (
            musinsa_brand == candidate_brand
            or musinsa_brand in candidate_brand
            or candidate_brand in musinsa_brand
        )
    )

    style_codes = extract_style_codes(musinsa.get("serial_number"), musinsa.get("name"))
    compact_candidate = alnum_compact(candidate_text(candidate))
    style_matches = [
        code
        for code in style_codes
        if alnum_compact(code) and alnum_compact(code) in compact_candidate
    ]

    n_sim = name_similarity(musinsa.get("name"), candidate.get("name_ko"))
    overlap = token_overlap(musinsa.get("name"), candidate.get("name_ko"))

    if style_codes and not style_matches:
        return 0.0, f"style code mismatch: expected one of {style_codes}"

    if musinsa_brand and candidate_brand and not brand_match and not style_matches:
        return 0.0, f"brand mismatch: {musinsa_brand} != {candidate_brand}"

    if style_codes:
        score = 0.62
        if brand_match:
            score += 0.16
        score += min(n_sim, 1.0) * 0.12
        score += min(overlap, 1.0) * 0.10
    else:
        score = 0.0
        if brand_match:
            score += 0.34
        score += min(n_sim, 1.0) * 0.38
        score += min(overlap, 1.0) * 0.28

    reason = (
        f"style_matches={style_matches or []}, brand_match={brand_match}, "
        f"name_similarity={n_sim:.2f}, token_overlap={overlap:.2f}"
    )
    return min(score, 1.0), reason


def local_match_decision(musinsa: dict[str, Any], candidates: list[dict[str, Any]]) -> MatchDecision:
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for candidate in candidates:
        score, reason = score_candidate(musinsa, candidate)
        scored.append((score, reason, candidate))

    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored or scored[0][0] <= 0:
        return MatchDecision(False, None, 0.0, "local", "no candidate passed hard filters")

    best_score, best_reason, best_candidate = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    threshold = 0.74 if extract_style_codes(musinsa.get("serial_number"), musinsa.get("name")) else 0.88

    if best_score >= threshold and best_score - second_score >= 0.10:
        return MatchDecision(True, best_candidate, best_score, "local", best_reason)

    return MatchDecision(
        False,
        best_candidate,
        best_score,
        "local_uncertain",
        f"best score below threshold or too close to another candidate: {best_reason}",
    )


def openai_enabled() -> bool:
    load_local_env()
    return os.getenv("OPENAI_MATCHING_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def embedding_enabled() -> bool:
    load_local_env()
    value = os.getenv("OPENAI_EMBEDDING_MATCHING_ENABLED")
    if value is None:
        value = os.getenv("OPENAI_MATCHING_ENABLED", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _embedding_db_path() -> str:
    from shop_comparator.core.config import DB_FILE
    from shop_comparator.core.utils import get_persistent_path

    return get_persistent_path(DB_FILE)


def _ensure_embedding_cache_table() -> None:
    with sqlite3.connect(_embedding_db_path(), timeout=10) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS product_match_embeddings (
                cache_key TEXT PRIMARY KEY,
                model TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                text_hash TEXT NOT NULL,
                embedding_json TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def _embedding_cache_key(text: str, model: str, dimensions: int) -> tuple[str, str]:
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cache_key = hashlib.sha256(f"{model}:{dimensions}:{text_hash}".encode("utf-8")).hexdigest()
    return cache_key, text_hash


def _cached_embedding(text: str, model: str, dimensions: int) -> list[float] | None:
    _ensure_embedding_cache_table()
    cache_key, _ = _embedding_cache_key(text, model, dimensions)
    with sqlite3.connect(_embedding_db_path(), timeout=10) as conn:
        row = conn.execute(
            "SELECT embedding_json FROM product_match_embeddings WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
    if not row:
        return None
    try:
        return [float(value) for value in json.loads(row[0])]
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _save_embedding(text: str, model: str, dimensions: int, embedding: list[float]) -> None:
    _ensure_embedding_cache_table()
    cache_key, text_hash = _embedding_cache_key(text, model, dimensions)
    with sqlite3.connect(_embedding_db_path(), timeout=10) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO product_match_embeddings (
                cache_key, model, dimensions, text_hash, embedding_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (cache_key, model, dimensions, text_hash, json.dumps(embedding)),
        )
        conn.commit()


def product_embedding_text(product: dict[str, Any], source: str) -> str:
    if source == "musinsa":
        brand = product.get("brand") or ""
        parts = [
            f"brand {normalize_brand(brand)} {brand}",
            f"product {product.get('name') or ''}",
            f"model {product.get('serial_number') or ''}",
        ]
    else:
        brand = product.get("brand") or ""
        parts = [
            f"brand {normalize_brand(brand)} {brand}",
            f"product {product.get('name_ko') or ''}",
            f"model {product.get('all_text') or ''}",
        ]
    return "\n".join(parts)


def embedding_for_text(text: str, timeout_sec: int = 20) -> list[float] | None:
    load_local_env()
    if not embedding_enabled():
        return None

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or not api_key.startswith("sk-"):
        return None

    model = os.getenv("OPENAI_EMBEDDING_MODEL", DEFAULT_OPENAI_EMBEDDING_MODEL)
    dimensions = int(os.getenv("OPENAI_EMBEDDING_DIMENSIONS", DEFAULT_OPENAI_EMBEDDING_DIMENSIONS))
    cached = _cached_embedding(text, model, dimensions)
    if cached is not None:
        return cached

    response = requests.post(
        "https://api.openai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "input": text,
            "encoding_format": "float",
            "dimensions": dimensions,
        },
        timeout=timeout_sec,
    )
    response.raise_for_status()
    embedding = [float(value) for value in response.json()["data"][0]["embedding"]]
    _save_embedding(text, model, dimensions, embedding)
    return embedding


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def embedding_match_decision(
    musinsa: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> MatchDecision | None:
    if not embedding_enabled() or not candidates:
        return None

    max_candidates = int(os.getenv("OPENAI_EMBEDDING_MAX_CANDIDATES", "8"))
    accept_threshold = float(os.getenv("OPENAI_EMBEDDING_ACCEPT_THRESHOLD", "0.88"))
    margin_threshold = float(os.getenv("OPENAI_EMBEDDING_MARGIN_THRESHOLD", "0.04"))

    viable: list[tuple[dict[str, Any], float, str]] = []
    for candidate in candidates[:max_candidates]:
        local_score, local_reason = score_candidate(musinsa, candidate)
        if local_score > 0:
            viable.append((candidate, local_score, local_reason))

    if not viable:
        return None

    try:
        musinsa_embedding = embedding_for_text(product_embedding_text(musinsa, "musinsa"))
        if musinsa_embedding is None:
            return None

        scored: list[tuple[float, float, str, dict[str, Any]]] = []
        for candidate, local_score, local_reason in viable:
            candidate_embedding = embedding_for_text(product_embedding_text(candidate, "kream"))
            if candidate_embedding is None:
                continue
            similarity = cosine_similarity(musinsa_embedding, candidate_embedding)
            scored.append((similarity, local_score, local_reason, candidate))
    except Exception as exc:
        return MatchDecision(False, None, 0.0, "embedding_error", f"Embedding match check failed: {exc}")

    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return None

    best_similarity, best_local_score, best_local_reason, best_candidate = scored[0]
    second_similarity = scored[1][0] if len(scored) > 1 else 0.0
    margin = best_similarity - second_similarity
    reason = (
        f"embedding_similarity={best_similarity:.3f}, margin={margin:.3f}, "
        f"local_score={best_local_score:.3f}, {best_local_reason}"
    )

    if best_similarity >= accept_threshold and margin >= margin_threshold:
        return MatchDecision(True, best_candidate, best_similarity, "embedding", reason)

    return MatchDecision(
        False,
        None,
        0.0,
        "embedding_uncertain",
        f"no embedding candidate crossed threshold: {reason}",
    )


def _response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    chunks: list[str] = []
    for item in payload.get("output") or []:
        for content in item.get("content") or []:
            if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "".join(chunks)


def openai_match_decision(
    musinsa: dict[str, Any],
    candidates: list[dict[str, Any]],
    timeout_sec: int = 25,
) -> MatchDecision | None:
    load_local_env()
    if not openai_enabled():
        return None

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    if not api_key.startswith("sk-"):
        return MatchDecision(
            False,
            None,
            0.0,
            "openai_config_error",
            "OPENAI_API_KEY is set but does not look like an OpenAI API key.",
        )

    model = os.getenv("OPENAI_MATCH_MODEL", DEFAULT_OPENAI_MATCH_MODEL)
    max_candidates = int(os.getenv("OPENAI_MATCH_MAX_CANDIDATES", "5"))
    max_output_tokens = int(os.getenv("OPENAI_MATCH_MAX_OUTPUT_TOKENS", DEFAULT_OPENAI_MATCH_MAX_OUTPUT_TOKENS))
    use_images = os.getenv("OPENAI_MATCH_USE_IMAGES", "1").strip().lower() not in {"0", "false", "no"}
    candidates = candidates[:max_candidates]

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decision": {"type": "string", "enum": ["match", "no_match", "uncertain"]},
            "matched_product_id": {"type": "string"},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["decision", "matched_product_id", "confidence", "reason"],
    }

    user_text = {
        "task": "Decide whether any KREAM search candidate is the same product as the Musinsa product.",
        "rules": [
            "Prefer exact model/style code matches.",
            "Reject candidates with a different brand, model number, silhouette, colorway, or collaboration.",
            "If evidence is insufficient, return uncertain or no_match.",
            "Return match only when confidence is at least 0.82.",
        ],
        "musinsa": musinsa,
        "candidates": candidates,
    }

    content: list[dict[str, Any]] = [{"type": "input_text", "text": json.dumps(user_text, ensure_ascii=False)}]
    if use_images and musinsa.get("image_url"):
        content.append({"type": "input_text", "text": "Musinsa product image"})
        content.append({"type": "input_image", "image_url": musinsa["image_url"]})
    if use_images:
        for candidate in candidates:
            if candidate.get("image_url"):
                content.append({"type": "input_text", "text": f"KREAM candidate {candidate.get('id')} image"})
                content.append({"type": "input_image", "image_url": candidate["image_url"]})

    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": (
                    "You are a conservative product matching verifier for Korean fashion marketplaces. "
                    "Use only the supplied text and images. Do not guess."
                ),
            },
            {"role": "user", "content": content},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "product_match_decision",
                "strict": True,
                "schema": schema,
            }
        },
        "reasoning": {"effort": "low"},
        "max_output_tokens": max_output_tokens,
        "store": False,
    }

    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout_sec,
        )
        response.raise_for_status()
        result = json.loads(_response_text(response.json()))
    except Exception as exc:
        return MatchDecision(False, None, 0.0, "openai_error", f"OpenAI match check failed: {exc}")

    decision = str(result.get("decision") or "uncertain")
    matched_id = str(result.get("matched_product_id") or "").strip()
    confidence = float(result.get("confidence") or 0.0)
    reason = str(result.get("reason") or "")
    product = next((candidate for candidate in candidates if str(candidate.get("id")) == matched_id), None)

    if decision == "match" and product and confidence >= 0.82:
        return MatchDecision(True, product, confidence, "openai", reason)

    return MatchDecision(
        False,
        None,
        0.0,
        "openai",
        f"{reason or decision} (decision={decision}, confidence={confidence:.2f})",
    )


def select_best_kream_match(
    musinsa: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> MatchDecision:
    local = local_match_decision(musinsa, candidates)
    if local.matched:
        return local

    embedding = embedding_match_decision(musinsa, candidates)
    if embedding is not None and embedding.matched:
        return embedding

    ai = openai_match_decision(musinsa, candidates)
    if ai is not None:
        return ai

    if embedding is not None:
        return embedding

    return local

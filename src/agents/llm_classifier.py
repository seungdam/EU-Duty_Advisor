"""ASAP experimental LLM classifier backed by the active ``cn_table``.

This module is intentionally small and adapter-shaped.  It keeps the public
``classify(product_input, top_k=...)`` function used by smoke tests while
reading the current project ``cn_table`` from Supabase only.
"""

from __future__ import annotations

import json
import math
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import psycopg2
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_local_env() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()

CN_TABLE_NAME = os.environ.get("ASAP_CN_TABLE_NAME", "cn_table")
LLM_MODEL = os.environ.get("EU_EXPORT_LLM_MODEL", "gemma4-ctx")
OLLAMA_ENDPOINT = (
    os.environ.get("EU_EXPORT_OLLAMA_ENDPOINT_URL")
    or os.environ.get("OLLAMA_HOST")
    or "http://localhost:11434"
).rstrip("/")
EMBED_MODEL_NAME = os.environ.get(
    "ASAP_ENGINE_EMBED_MODEL",
    "paraphrase-multilingual-mpnet-base-v2",
)
TOP_PREFILTER_ROWS = int(os.environ.get("ASAP_ENGINE_PREFILTER_ROWS", "450"))
LLM_CANDIDATE_COUNT = int(os.environ.get("ASAP_ENGINE_LLM_CANDIDATES", "12"))
LLM_TIMEOUT_SECONDS = int(os.environ.get("ASAP_ENGINE_LLM_TIMEOUT_SECONDS", "600"))
LAST_CLASSIFY_TRACE: dict[str, Any] = {}


TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")
JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```|(\{.*\})", re.DOTALL)
KO_QUERY_HINTS: tuple[tuple[str, str], ...] = (
    ("미역국", "seaweed soup broth prepared soup"),
    ("재첩국", "corbicula clam soup broth prepared soup"),
    ("오징어무국", "squid radish soup broth prepared soup"),
    ("국", "soup broth prepared soup"),
    ("탕", "soup broth prepared soup"),
    ("찌개", "soup stew prepared soup"),
    ("볶음", "stir fried cooked seasoned prepared preserved"),
    ("무침", "seasoned mixed prepared preserved"),
    ("비빔장", "seasoned marinated prepared preserved sauce"),
    ("라면", "instant noodles pasta dried noodles"),
    ("칼국수", "wheat noodles pasta uncooked noodles"),
    ("밀면", "wheat noodles pasta uncooked noodles"),
    ("메밀", "buckwheat noodles soba pasta"),
    ("비빔면", "mixed spicy noodles pasta"),
    ("면", "noodles pasta"),
    ("만두", "dumpling stuffed pasta"),
    ("청양고추", "fresh green chilli pepper vegetable capsicum"),
    ("고추", "fresh chilli pepper vegetable capsicum"),
    ("쪽갈비", "pork ribs swine meat ribs"),
    ("등갈비", "pork spare ribs swine meat ribs"),
    ("갈비", "ribs meat pork beef"),
    ("돼지", "pork swine meat"),
    ("한돈", "pork swine meat"),
    ("소고기", "beef bovine meat"),
    ("닭", "chicken poultry meat"),
    ("낙지", "octopus mollusc aquatic invertebrate prepared preserved"),
    ("주꾸미", "octopus aquatic invertebrate prepared seafood"),
    ("쭈꾸미", "octopus aquatic invertebrate prepared seafood"),
    ("꼬막", "cockle mollusc prepared seafood"),
    ("새우", "shrimp prawn crustacean prepared seafood"),
    ("멘보샤", "shrimp toast prepared crustacean bread"),
    ("소스", "sauce condiment preparation"),
    ("양념", "sauce seasoning condiment preparation"),
    ("장", "sauce paste condiment preparation"),
    ("김치", "kimchi fermented vegetable preparation"),
)


def _tokenize(text: str) -> set[str]:
    return {
        token.lower()
        for token in TOKEN_RE.findall(text or "")
        if len(token) >= 2
    }


def _expand_query(text: str) -> str:
    additions = [
        english
        for korean, english in KO_QUERY_HINTS
        # Single-character Korean hints such as "국", "면", and "장" create too
        # many false positives in OCR/COI evidence: 국내산, 미국산, 작성표, etc.
        if len(korean) >= 2 and korean in (text or "")
    ]
    if not additions:
        return text
    return "\n".join([text, "customs keyword expansion: " + "; ".join(additions)])


def _read(row: dict[str, Any], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _candidate_code(row: dict[str, Any]) -> str:
    return "".join(ch for ch in _read(row, "cn", "cn8", "hs8") if ch.isdigit())[:8]


def _hs6(row: dict[str, Any]) -> str:
    code = "".join(ch for ch in _read(row, "subheading", "hs6_code", "hs6") if ch.isdigit())
    if len(code) >= 6:
        return code[:6]
    return _candidate_code(row)[:6]


def _candidate_description(row: dict[str, Any]) -> str:
    parts = [
        _read(row, "heading_description"),
        _read(row, "subheading_description"),
        _read(row, "cn_description"),
        _read(row, "combined_description"),
        _read(row, "branch_context"),
    ]
    return " | ".join(part for part in parts if part)


def _candidate_search_text(row: dict[str, Any]) -> str:
    fields = [
        "heading_description",
        "heading_including",
        "heading_excluding",
        "heading_keywords",
        "subheading_description",
        "subheading_keywords",
        "cn_description",
        "cn_keywords",
        "cn_explanatory_note",
        "branch_context",
        "branch_keywords",
        "cn_note_keywords",
        "include_rule_keywords",
        "exclude_rule_keywords",
        "hard_conditions",
        "combined_description",
        "search_keywords",
    ]
    return "\n".join(_read(row, field) for field in fields if _read(row, field))


def _db_connect():
    host = os.environ.get("PGHOST") or ""
    if "supabase.com" not in host:
        raise RuntimeError(
            "engine.classifier is Supabase-only. Set Supabase PGHOST/"
            "PGDATABASE/PGUSER/PGPASSWORD/PGPORT with PGSSLMODE=require."
        )
    missing = [
        key
        for key in ("PGDATABASE", "PGUSER", "PGPASSWORD")
        if not os.environ.get(key)
    ]
    if missing:
        raise RuntimeError(f"Missing Supabase PG env var(s): {', '.join(missing)}")
    return psycopg2.connect(
        host=host,
        port=os.environ.get("PGPORT") or "5432",
        dbname=os.environ.get("PGDATABASE"),
        user=os.environ.get("PGUSER"),
        password=os.environ.get("PGPASSWORD"),
        sslmode=os.environ.get("PGSSLMODE") or "require",
        connect_timeout=15,
    )


@lru_cache(maxsize=1)
def _load_cn_rows() -> tuple[dict[str, Any], ...]:
    """Load active CN rows from Supabase."""

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", CN_TABLE_NAME):
        raise RuntimeError(f"Unsafe CN table name: {CN_TABLE_NAME!r}")
    with _db_connect() as conn, conn.cursor() as cur:
        cur.execute(f"select * from {CN_TABLE_NAME}")
        columns = [desc[0] for desc in cur.description]
        rows = [dict(zip(columns, values)) for values in cur.fetchall()]
    if not rows:
        raise RuntimeError(f"Supabase table {CN_TABLE_NAME!r} returned 0 rows.")
    print(f"[engine] cn_table loaded from supabase rows={len(rows)}", flush=True)
    return tuple(rows)


@lru_cache(maxsize=1)
def _embed_model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(EMBED_MODEL_NAME)


def _embed(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    model = _embed_model()
    vectors = model.encode(
        [text[:2000] for text in texts],
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return vectors.tolist() if hasattr(vectors, "tolist") else vectors


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _keyword_boost(expanded_query: str, row: dict[str, Any]) -> float:
    query = expanded_query.lower()
    text = _candidate_search_text(row).lower()
    cn_description = _read(row, "cn_description", "subheading_description").lower()
    hs6 = _hs6(row)
    boost = 0.0
    prepared_marker = any(
        word in query
        for word in (
            "prepared",
            "preserved",
            "cooked",
            "fried",
            "stir fried",
            "stir-fried",
            "seasoned",
            "marinated",
        )
    )
    aquatic_marker = any(
        word in query
        for word in ("octopus", "mollusc", "cockle", "squid", "shrimp", "prawn", "crustacean")
    )
    if any(word in query for word in ("soup", "broth", "stew")):
        if hs6 == "210410":
            boost += 0.85
        if "soup" in text or "broth" in text:
            boost += 0.35
        if "sauce" in text or "condiment" in text:
            boost -= 0.35
    if any(word in query for word in ("noodle", "noodles", "pasta", "buckwheat")):
        if hs6 in {"190219", "190230"}:
            boost += 0.65
        if "pasta" in text or "noodle" in text:
            boost += 0.25
    if any(word in query for word in ("chilli", "chili", "pepper", "capsicum")):
        if hs6 in {"070960", "071080", "090421", "090422"}:
            boost += 0.75
        if "capsicum" in text or "pepper" in text:
            boost += 0.25
    if any(word in query for word in ("pork ribs", "spare ribs", "swine meat ribs", "ribs")):
        if hs6 in {"020319", "020329", "160249"}:
            boost += 0.55
        if "swine" in text or "pork" in text or "ribs" in text:
            boost += 0.25
    if any(word in query for word in ("shrimp", "prawn", "crustacean")):
        if hs6 in {"160521", "160529", "030617", "030616"}:
            boost += 0.55
    if any(word in query for word in ("octopus", "mollusc", "cockle")):
        if hs6.startswith("1605") or hs6.startswith("0307"):
            boost += 0.5
        if "octopus" in query and "octopus" in cn_description:
            boost += 0.65
        if "cockle" in query and ("cockle" in cn_description or "clam" in cn_description):
            boost += 0.45
    if aquatic_marker and prepared_marker:
        if hs6.startswith("1605"):
            boost += 0.85
        if hs6.startswith("0306") or hs6.startswith("0307") or hs6.startswith("0308"):
            boost -= 0.35
    return boost


def _lexical_prefilter(query: str, rows: Iterable[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    query_tokens = _tokenize(query)
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for row in rows:
        code = _candidate_code(row)
        if len(code) != 8:
            continue
        text = _candidate_search_text(row)
        tokens = _tokenize(text)
        overlap = len(query_tokens & tokens)
        exact_bonus = 0
        q_lower = query.lower()
        for keyword in ("soup", "broth", "noodle", "pasta", "pepper", "chilli", "chili", "rib", "pork", "seaweed"):
            if keyword in q_lower and keyword in text.lower():
                exact_bonus += 3
        score = overlap + exact_bonus
        if score > 0:
            scored.append((float(score), code, row))

    if not scored:
        return list(rows)[:limit]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [row for _, _, row in scored[:limit]]


def search_cn_candidates(product_input: str, top_k: int = 40) -> list[dict[str, Any]]:
    rows = _load_cn_rows()
    expanded_input = _expand_query(product_input)
    prefiltered = _lexical_prefilter(expanded_input, rows, TOP_PREFILTER_ROWS)
    candidate_texts = [_candidate_search_text(row) for row in prefiltered]
    query_tokens = _tokenize(expanded_input)

    query_vec: list[float] | None = None
    candidate_vecs: list[list[float]] | None = None
    try:
        query_vec = _embed([expanded_input])[0]
        candidate_vecs = _embed(candidate_texts)
    except Exception as exc:  # noqa: BLE001 - offline smoke fallback
        print(f"[engine] embedding unavailable; lexical fallback: {exc}", flush=True)

    ranked: list[dict[str, Any]] = []
    for idx, row in enumerate(prefiltered):
        code = _candidate_code(row)
        hs6 = _hs6(row)
        if len(code) != 8 or len(hs6) != 6:
            continue
        if query_vec is not None and candidate_vecs is not None:
            semantic_similarity = _cosine(query_vec, candidate_vecs[idx])
        else:
            text_tokens = _tokenize(candidate_texts[idx])
            semantic_similarity = min(1.0, len(query_tokens & text_tokens) / 8.0)
        boost = _keyword_boost(expanded_input, row)
        similarity = semantic_similarity + boost
        ranked.append(
            {
                "taric_code_10": code,
                "cn8": code,
                "hs6": hs6,
                "chapter": _read(row, "chapter"),
                "description": _read(row, "cn_description", "subheading_description"),
                "full_description": _candidate_description(row),
                "rule_context": _candidate_search_text(row)[:1200],
                "similarity": similarity,
                "semantic_similarity": semantic_similarity,
                "keyword_boost": boost,
            }
        )

    best_by_hs6: dict[str, dict[str, Any]] = {}
    for item in sorted(ranked, key=lambda value: (-value["similarity"], value["cn8"])):
        best_by_hs6.setdefault(item["hs6"], item)
    return list(best_by_hs6.values())[:top_k]


def llm(prompt: str, max_tokens: int = 768) -> str:
    try:
        response = requests.post(
            f"{OLLAMA_ENDPOINT}/api/chat",
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "format": "json",
                "think": False,
                "options": {"num_predict": max_tokens, "temperature": 0},
            },
            timeout=LLM_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json().get("message", {}).get("content", "").strip()
    except Exception as exc:  # noqa: BLE001
        return f"[LLM 오류: {exc}]"


def parse_json(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    match = JSON_RE.search(raw)
    if not match:
        return {}
    payload = match.group(1) or match.group(2)
    try:
        data = json.loads(payload)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _llm_select(product_input: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    decision, _trace = _llm_select_with_trace(product_input, candidates)
    return decision


def _llm_select_with_trace(
    product_input: str,
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    expanded_input = _expand_query(product_input)
    candidate_payload = [
        {
            "hs6": row["hs6"],
            "cn8": row["cn8"],
            "description": row["full_description"][:500],
            "keywords_or_rules": row["rule_context"][:500],
            "embedding_similarity": round(float(row["similarity"]), 4),
        }
        for row in candidates[:LLM_CANDIDATE_COUNT]
    ]
    prompt = "\n".join(
        [
            "You are classifying goods for EU CN/HS purposes.",
            "Choose only from the provided cn_table candidates. Do not invent codes.",
            "The product evidence may contain Korean OCR text; use it directly.",
            "Return JSON only.",
            'Shape: {"ranked":[{"hs6":"string","cn8":"string","confidence":0.0,"match":true,"reason":"short"}]}',
            "",
            "PRODUCT_EVIDENCE:",
            expanded_input[:1800],
            "",
            "CN_TABLE_CANDIDATES:",
            json.dumps(candidate_payload, ensure_ascii=False),
        ]
    )
    raw_response = llm(prompt, max_tokens=1024)
    parsed = parse_json(raw_response)
    trace = {
        "model": LLM_MODEL,
        "ollama_endpoint": OLLAMA_ENDPOINT,
        "prompt": prompt,
        "prompt_length": len(prompt),
        "expanded_input": expanded_input,
        "candidate_payload": candidate_payload,
        "raw_response": raw_response,
        "raw_response_length": len(raw_response),
        "parse_ok": bool(isinstance(parsed.get("ranked"), list)),
    }
    return parsed, trace


def get_last_trace() -> dict[str, Any]:
    """Return the most recent ``classify`` trace for smoke-test inspection."""

    return dict(LAST_CLASSIFY_TRACE)


def classify(product_input: str, top_k: int = 5, chapter_hint: str | None = None) -> list[dict[str, Any]]:
    """Return classifier rows compatible with ``classifier_ab_smoke``.

    ``chapter_hint`` is accepted for backward compatibility; this cn_table engine
    currently lets embedding + LLM choose from active candidates instead.
    """

    print(f"\n{'=' * 60}")
    print(f"[분류 시작] {product_input[:120]!r}")
    print(f"  cn_table 검색 source={CN_TABLE_NAME} model={LLM_MODEL}", flush=True)

    candidates = search_cn_candidates(product_input, top_k=max(LLM_CANDIDATE_COUNT, top_k))
    if not candidates:
        return []

    decision, trace = _llm_select_with_trace(product_input, candidates)
    ranked = decision.get("ranked") if isinstance(decision, dict) else None
    output: list[dict[str, Any]] = []
    candidate_by_code = {row["cn8"]: row for row in candidates}
    candidate_by_hs6 = {row["hs6"]: row for row in candidates}

    if isinstance(ranked, list):
        for item in ranked:
            if not isinstance(item, dict):
                continue
            code = "".join(ch for ch in str(item.get("cn8") or "") if ch.isdigit())[:8]
            hs6 = "".join(ch for ch in str(item.get("hs6") or "") if ch.isdigit())[:6]
            row = candidate_by_code.get(code) or candidate_by_hs6.get(hs6)
            if row is None:
                continue
            confidence = item.get("confidence", row["similarity"])
            try:
                confidence_float = float(confidence)
            except (TypeError, ValueError):
                confidence_float = float(row["similarity"])
            output.append(
                {
                    "no": len(output) + 1,
                    "결정세번": row["cn8"],
                    "hs6": row["hs6"],
                    "물품설명_원문": row["full_description"],
                    "물품설명_한글": "",
                    "분류사유_영문": str(item.get("reason") or "")[:500],
                    "분류사유_한글": "",
                    "유사도_nom": round(float(row["similarity"]), 3),
                    "신뢰도": round(max(0.0, min(1.0, confidence_float)), 3),
                    "검증_상태": "확정" if item.get("match") is not False else "재검토필요",
                    "분류_단계": "cn_table_llm",
                    "bti_참조": "",
                    "bti_국가": "",
                    "bti_유사도": None,
                    "적용법령": "",
                    "cn_note_있음": bool(row.get("rule_context")),
                }
            )
            if len(output) >= top_k:
                break

    if not output:
        trace["fallback_used"] = True
        for row in candidates[:top_k]:
            output.append(
                {
                    "no": len(output) + 1,
                    "결정세번": row["cn8"],
                    "hs6": row["hs6"],
                    "물품설명_원문": row["full_description"],
                    "물품설명_한글": "",
                    "분류사유_영문": "Fallback to cn_table embedding rank; LLM returned no parseable candidate.",
                    "분류사유_한글": "",
                    "유사도_nom": round(float(row["similarity"]), 3),
                    "신뢰도": round(max(0.0, min(1.0, float(row["similarity"]))), 3),
                    "검증_상태": "재검토필요",
                    "분류_단계": "cn_table_embedding_fallback",
                    "bti_참조": "",
                    "bti_국가": "",
                    "bti_유사도": None,
                    "적용법령": "",
                    "cn_note_있음": bool(row.get("rule_context")),
                }
            )
    else:
        trace["fallback_used"] = False

    trace["output_hs6"] = [str(row.get("hs6") or "")[:6] for row in output]
    trace["output_cn8"] = [str(row.get("결정세번") or "")[:8] for row in output]
    LAST_CLASSIFY_TRACE.clear()
    LAST_CLASSIFY_TRACE.update(trace)
    return output[:top_k]


if __name__ == "__main__":
    import sys

    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "seaweed soup"
    for result in classify(query):
        print(json.dumps(result, ensure_ascii=False, indent=2))

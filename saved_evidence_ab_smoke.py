"""A/B smoke for saved KurlyGlobal evidence + COI.

This avoids live URL collection while KurlyGlobal is rate-limited.  For each
saved product page row, it compares:

  expected HS6 -> engine.classifier LLM answer -> export_eu retriever answer

Artifacts include a compact blackboard-like payload so we can inspect what
each classifier saw and returned.

Environment:
  ASAP_SAVED_AB_LIMIT=N          first N cases; 0 means all, default 10
  ASAP_SAVED_AB_TOP_K=N          candidate count, default 5
  ASAP_SAVED_AB_INCLUDE_COI=0    disable COI evidence, default on
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parent
SRC_ROOT = PROJECT_ROOT / "src"
for extraPath in (PROJECT_ROOT, SRC_ROOT):
    extraPathText = str(extraPath)
    if extraPathText in sys.path:
        sys.path.remove(extraPathText)
if PROJECT_ROOT.exists():
    sys.path.insert(0, str(PROJECT_ROOT))
if SRC_ROOT.exists():
    sys.path.insert(0, str(SRC_ROOT))

os.environ.setdefault("ASAP_PROJECT_ROOT", str(PROJECT_ROOT))

RENDER_SUMMARY_PATH = PROJECT_ROOT / "artifacts" / "kurlyglobal-smoke" / "render-smoke-summary.json"
ANSWER_PATH = PROJECT_ROOT / "test" / "answer.csv"
ARTIFACT_DIR = PROJECT_ROOT / "artifacts" / "classifier-ab"
TOP_K = int(os.environ.get("ASAP_SAVED_AB_TOP_K", "5") or "5")
LIMIT = int(os.environ.get("ASAP_SAVED_AB_LIMIT", "10") or "10")
INCLUDE_COI = os.environ.get("ASAP_SAVED_AB_INCLUDE_COI", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
DOMAIN_SCOPE = os.environ.get("ASAP_SAVED_AB_DOMAIN_SCOPE", "food_16_21")


def _norm_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _normalize_hs_code(value: object) -> str:
    hs = "".join(ch for ch in str(value or "") if ch.isdigit())
    # Leading-zero HS codes can be converted to numeric strings by spreadsheets.
    if len(hs) == 9:
        hs = "0" + hs
    return hs


def _load_answers() -> dict[str, str]:
    answers: dict[str, str] = {}
    with ANSWER_PATH.open(newline="", encoding="utf-8-sig") as file:
        for row in csv.DictReader(file):
            url = _norm_url(row.get("상품 상세", ""))
            hs = _normalize_hs_code(row.get("미국 HS Code", ""))
            if url and len(hs) >= 6:
                answers[url] = hs[:6]
    return answers


def _load_saved_rows() -> list[dict[str, Any]]:
    data = json.loads(RENDER_SUMMARY_PATH.read_text(encoding="utf-8"))
    answers = _load_answers()
    rows: list[dict[str, Any]] = []
    for item in data.get("results", []):
        if not isinstance(item, dict):
            continue
        normalized_url = _norm_url(str(item.get("url") or ""))
        expected = answers.get(normalized_url)
        heading = str(item.get("heading") or "").strip()
        title = str(item.get("title") or "").strip()
        if not expected or not heading:
            continue
        rows.append(
            {
                "index": item.get("index"),
                "url": item.get("url"),
                "normalized_url": normalized_url,
                "product_name": heading,
                "page_title": title,
                "expected_hs6": expected,
            }
        )
    if LIMIT > 0:
        rows = rows[:LIMIT]
    return rows


def _build_case_evidence(row: dict[str, Any]) -> dict[str, Any]:
    from agents.coi_loader import load_coi_evidence

    parts = [
        f"product_name: {row['product_name']}",
        f"page_title: {row['page_title']}" if row.get("page_title") else "",
    ]
    coi_path = ""
    coi_text = ""
    if INCLUDE_COI:
        case_index = int(row["index"]) if str(row.get("index") or "").isdigit() else None
        coi = load_coi_evidence(
            case_index=case_index,
            product_name=str(row.get("product_name") or ""),
        )
        if coi is not None:
            coi_path = str(coi.path)
            coi_text = coi.text
            parts.extend([f"COI evidence from {coi.path.name}:", coi.text])

    evidence = "\n".join(part for part in parts if part).strip()
    composition = [line for line in [row.get("page_title") or "", coi_text[:5000]] if line]
    observed_facts = {
        "product_name": row["product_name"],
        "description": row.get("page_title") or "",
        "composition": composition,
        "ocr_text": [evidence],
        "coi_path": coi_path,
        "source_urls": [row["normalized_url"]],
        "origin_country": "KR",
        "intended_use": "food_import_classification_smoke",
    }
    return {
        "evidence_text": evidence,
        "coi_path": coi_path,
        "coi_text_length": len(coi_text),
        "observed_facts": observed_facts,
    }


@lru_cache(maxsize=1)
def _team_retriever():
    from agents._external_classifier import ASAP_ONTOLOGY_ROOT, ASAP_PROJECT_ROOT
    from bussiness_logic.core import CnCandidateRetriever

    return CnCandidateRetriever(ASAP_ONTOLOGY_ROOT, ASAP_PROJECT_ROOT)


def _classify_team_export_eu(observed_facts: dict[str, Any]) -> list[dict[str, Any]]:
    from agents._external_classifier import pes_to_input

    product_input = pes_to_input({"observed_facts": observed_facts}, domain_scope=DOMAIN_SCOPE)
    candidates = _team_retriever().FindCandidates(product_input, topK=TOP_K)
    out: list[dict[str, Any]] = []
    for candidate in candidates:
        hs8 = str(getattr(candidate, "hs8", "") or "")
        out.append(
            {
                "cn8": hs8,
                "hs6": hs8[:6],
                "score": round(float(getattr(candidate, "score", 0.0) or 0.0), 4),
                "description": str(getattr(candidate, "hs8Description", "") or "")[:500],
                "matched_terms": list(getattr(candidate, "matchedTerms", []) or []),
                "retrieval_sources": list(getattr(candidate, "retrievalSources", []) or []),
            }
        )
    return out


def _classify_llm_engine(evidence_text: str) -> list[dict[str, Any]]:
    from agents.llm_classifier import classify

    rows = classify(evidence_text, top_k=TOP_K)
    out: list[dict[str, Any]] = []
    for row in rows:
        cn8 = str(row.get("결정세번") or "")[:8]
        out.append(
            {
                "cn8": cn8,
                "hs6": str(row.get("hs6") or cn8[:6])[:6],
                "confidence": row.get("신뢰도"),
                "status": row.get("검증_상태"),
                "stage": row.get("분류_단계"),
                "reason": row.get("분류사유_영문") or row.get("분류사유_한글") or "",
                "description": row.get("물품설명_원문") or "",
            }
        )
    return out


def _top_hs6(candidates: list[dict[str, Any]]) -> list[str]:
    return [
        hs6
        for hs6 in (str(candidate.get("hs6") or "")[:6] for candidate in candidates)
        if len(hs6) == 6
    ]


def _write_outputs(payload: dict[str, Any]) -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = ARTIFACT_DIR / "saved-evidence-ab-summary.json"
    csv_path = ARTIFACT_DIR / "saved-evidence-ab-results.csv"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "index",
                "product_name",
                "url",
                "expected_hs6",
                "llm_top1_hs6",
                "team_top1_hs6",
                "llm_top5_hs6",
                "team_top5_hs6",
                "llm_top1_match",
                "team_top1_match",
                "llm_top5_match",
                "team_top5_match",
                "coi_path",
                "llm_elapsed_seconds",
                "team_elapsed_seconds",
                "llm_error",
                "team_error",
            ],
        )
        writer.writeheader()
        for row in payload["results"]:
            writer.writerow(
                {
                    "index": row["index"],
                    "product_name": row["product_name"],
                    "url": row["url"],
                    "expected_hs6": row["expected_hs6"],
                    "llm_top1_hs6": row["llm_top1_hs6"],
                    "team_top1_hs6": row["team_top1_hs6"],
                    "llm_top5_hs6": " ".join(row["llm_top5_hs6"]),
                    "team_top5_hs6": " ".join(row["team_top5_hs6"]),
                    "llm_top1_match": row["llm_top1_match"],
                    "team_top1_match": row["team_top1_match"],
                    "llm_top5_match": row["llm_top5_match"],
                    "team_top5_match": row["team_top5_match"],
                    "coi_path": row.get("coi_path", ""),
                    "llm_elapsed_seconds": row["llm_elapsed_seconds"],
                    "team_elapsed_seconds": row["team_elapsed_seconds"],
                    "llm_error": row["llm_error"],
                    "team_error": row["team_error"],
                }
            )

    print(f"\nSaved JSON: {json_path}")
    print(f"Saved CSV : {csv_path}")


def main() -> int:
    rows = _load_saved_rows()
    print(
        f"saved rows={len(rows)} top_k={TOP_K} include_coi={INCLUDE_COI} "
        f"domain_scope={DOMAIN_SCOPE}"
    )

    results: list[dict[str, Any]] = []
    for ordinal, row in enumerate(rows, 1):
        evidence = _build_case_evidence(row)
        expected = row["expected_hs6"]

        llm_started = time.monotonic()
        try:
            llm_candidates = _classify_llm_engine(evidence["evidence_text"])
            llm_error = ""
        except Exception as exc:  # noqa: BLE001
            llm_candidates = []
            llm_error = str(exc)
        llm_elapsed = round(time.monotonic() - llm_started, 2)

        team_started = time.monotonic()
        try:
            team_candidates = _classify_team_export_eu(evidence["observed_facts"])
            team_error = ""
        except Exception as exc:  # noqa: BLE001
            team_candidates = []
            team_error = str(exc)
        team_elapsed = round(time.monotonic() - team_started, 2)

        llm_top5 = _top_hs6(llm_candidates)
        team_top5 = _top_hs6(team_candidates)
        llm_top1 = llm_top5[0] if llm_top5 else ""
        team_top1 = team_top5[0] if team_top5 else ""

        print(
            f"[{ordinal}/{len(rows)}] 답지={expected} -> "
            f"LLM={llm_top1 or '-'} ({'OK' if llm_top1 == expected else 'NO'}) -> "
            f"팀장={team_top1 or '-'} ({'OK' if team_top1 == expected else 'NO'}) | "
            f"{row['product_name'][:44]} | llm={llm_elapsed}s team={team_elapsed}s",
            flush=True,
        )

        blackboard_like = {
            "product_evidence_state": {
                "object_type": "ProductEvidenceState",
                "created_by": "saved_evidence_ab_smoke",
                "observed_facts": evidence["observed_facts"],
            },
            "llm_engine": {
                "input_preview": evidence["evidence_text"][:2500],
                "candidates": llm_candidates,
                "error": llm_error,
            },
            "export_eu_classifier": {
                "domain_scope": DOMAIN_SCOPE,
                "candidates": team_candidates,
                "error": team_error,
            },
        }

        results.append(
            {
                "index": row["index"],
                "url": row["url"],
                "product_name": row["product_name"],
                "page_title": row["page_title"],
                "expected_hs6": expected,
                "coi_path": evidence["coi_path"],
                "coi_text_length": evidence["coi_text_length"],
                "llm_candidates": llm_candidates,
                "team_candidates": team_candidates,
                "llm_top1_hs6": llm_top1,
                "team_top1_hs6": team_top1,
                "llm_top5_hs6": llm_top5,
                "team_top5_hs6": team_top5,
                "llm_top1_match": bool(llm_top1) and llm_top1 == expected,
                "team_top1_match": bool(team_top1) and team_top1 == expected,
                "llm_top5_match": expected in llm_top5,
                "team_top5_match": expected in team_top5,
                "llm_elapsed_seconds": llm_elapsed,
                "team_elapsed_seconds": team_elapsed,
                "llm_error": llm_error,
                "team_error": team_error,
                "blackboard": blackboard_like,
            }
        )

    total = len(results)
    payload = {
        "mode": "saved_evidence_coi_ab",
        "render_summary_path": str(RENDER_SUMMARY_PATH),
        "answer_path": str(ANSWER_PATH),
        "top_k": TOP_K,
        "limit": LIMIT,
        "include_coi": INCLUDE_COI,
        "total": total,
        "llm_top1": sum(1 for row in results if row["llm_top1_match"]),
        "llm_top5": sum(1 for row in results if row["llm_top5_match"]),
        "team_top1": sum(1 for row in results if row["team_top1_match"]),
        "team_top5": sum(1 for row in results if row["team_top5_match"]),
        "llm_errors": sum(1 for row in results if row["llm_error"]),
        "team_errors": sum(1 for row in results if row["team_error"]),
        "results": results,
    }
    for key in ("llm_top1", "llm_top5", "team_top1", "team_top5"):
        payload[f"{key}_accuracy"] = round(payload[key] / total, 4) if total else 0.0

    print(
        "\nSUMMARY "
        f"total={total} "
        f"LLM top1={payload['llm_top1']}/{total} ({payload['llm_top1_accuracy']*100:.1f}%) "
        f"top5={payload['llm_top5']}/{total} ({payload['llm_top5_accuracy']*100:.1f}%) | "
        f"TEAM top1={payload['team_top1']}/{total} ({payload['team_top1_accuracy']*100:.1f}%) "
        f"top5={payload['team_top5']}/{total} ({payload['team_top5_accuracy']*100:.1f}%) "
        f"errors llm={payload['llm_errors']} team={payload['team_errors']}"
    )
    _write_outputs(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

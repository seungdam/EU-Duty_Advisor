"""Run engine.classifier on the generated food product input testset.

This is an engine-only LLM smoke: no live URL collection and no team
``export_eu`` classifier.  It uses the structured food input CSV that contains
product descriptions and ingredients.

Outputs are rewritten after every row so long local LLM runs are resumable by
inspection even if interrupted.

Environment:
  ASAP_GENERATED_FOOD_DATASET=/path/to.csv
  ASAP_GENERATED_FOOD_LIMIT=N
  ASAP_GENERATED_FOOD_TOP_K=N
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any


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

DATASET_PATH = Path(
    os.environ.get(
        "ASAP_GENERATED_FOOD_DATASET",
        PROJECT_ROOT / "test" / "food_import_generated_product_inputs.csv",
    )
).expanduser()
ARTIFACT_DIR = PROJECT_ROOT / "artifacts" / "generated-food-llm"
BLACKBOARD_DIR = ARTIFACT_DIR / "blackboards"
TOP_K = int(os.environ.get("ASAP_GENERATED_FOOD_TOP_K", "5") or "5")
LIMIT = int(os.environ.get("ASAP_GENERATED_FOOD_LIMIT", "0") or "0")
DOMAIN_SCOPE = os.environ.get("ASAP_GENERATED_FOOD_DOMAIN_SCOPE", "food_16_21")


def _digits(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _hs6(value: object) -> str:
    digits = _digits(value)
    return digits[:6] if len(digits) >= 6 else ""


def _load_rows() -> list[dict[str, str]]:
    with DATASET_PATH.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    if LIMIT > 0:
        rows = rows[:LIMIT]
    return rows


def _build_evidence(row: dict[str, str]) -> str:
    parts = [
        f"product_name_ko: {row.get('korean_name', '').strip()}",
        f"product_name_en: {row.get('english_name_original', '').strip()}",
        "classification_input:",
        row.get("product_input_for_rag", "").strip(),
        "main_ingredients:",
        row.get("main_ingredients_original", "").strip(),
    ]
    return "\n".join(part for part in parts if part).strip()


def _top_hs6(candidates: list[dict[str, Any]]) -> list[str]:
    return [
        hs6
        for hs6 in (str(candidate.get("hs6") or "")[:6] for candidate in candidates)
        if len(hs6) == 6
    ]


@lru_cache(maxsize=1)
def _team_retriever():
    from agents._external_classifier import ASAP_ONTOLOGY_ROOT, ASAP_PROJECT_ROOT
    from bussiness_logic.core import CnCandidateRetriever

    return CnCandidateRetriever(ASAP_ONTOLOGY_ROOT, ASAP_PROJECT_ROOT)


def _classify_team(row: dict[str, str], evidence: str) -> list[dict[str, Any]]:
    from agents._external_classifier import pes_to_input

    product_input = pes_to_input(
        {
            "observed_facts": {
                "product_name": row.get("korean_name") or row.get("english_name_original") or "",
                "description": row.get("product_input_for_rag") or "",
                "composition": [
                    row.get("main_ingredients_original") or "",
                    row.get("english_name_original") or "",
                    evidence,
                ],
                "ocr_text": [evidence],
                "source_urls": [],
                "origin_country": "KR",
                "intended_use": "food_import_classification_smoke",
            }
        },
        domain_scope=DOMAIN_SCOPE,
    )
    candidates = _team_retriever().FindCandidates(product_input, topK=TOP_K)
    out: list[dict[str, Any]] = []
    for idx, candidate in enumerate(candidates, 1):
        cn8 = str(getattr(candidate, "hs8", "") or getattr(candidate, "hs8Code", "") or "")
        hs6 = cn8[:6]
        out.append(
            {
                "rank": idx,
                "cn8": cn8,
                "hs6": hs6,
                "score": round(float(getattr(candidate, "score", 0.0) or 0.0), 4),
                "description": str(getattr(candidate, "hs8Description", "") or "")[:500],
                "matched_terms": list(getattr(candidate, "matchedTerms", []) or []),
                "retrieval_sources": list(getattr(candidate, "retrievalSources", []) or []),
            }
        )
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _candidate_objects(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, candidate in enumerate(candidates, 1):
        cn8 = str(candidate.get("결정세번") or "")[:8]
        hs6 = str(candidate.get("hs6") or cn8[:6])[:6]
        out.append(
            {
                "object_type": "CandidateCode",
                "candidate_id": f"cand_{idx:03d}",
                "hs6": hs6,
                "cn8": cn8,
                "taric10": "",
                "rank": idx,
                "confidence": candidate.get("신뢰도"),
                "status": candidate.get("검증_상태"),
                "classification_basis": [
                    candidate.get("분류사유_영문")
                    or candidate.get("분류사유_한글")
                    or ""
                ],
                "classification_citations": [
                    {
                        "source_table": "cn_table",
                        "source_id": cn8 or hs6,
                        "snippet": str(candidate.get("물품설명_원문") or "")[:500],
                        "reason": "Candidate selected by engine.classifier from Supabase cn_table.",
                    }
                ],
                "raw_engine_row": candidate,
            }
        )
    return out


def _team_candidate_objects(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, candidate in enumerate(candidates, 1):
        cn8 = str(candidate.get("cn8") or "")[:8]
        hs6 = str(candidate.get("hs6") or cn8[:6])[:6]
        out.append(
            {
                "object_type": "CandidateCode",
                "candidate_id": f"team_cand_{idx:03d}",
                "hs6": hs6,
                "cn8": cn8,
                "taric10": "",
                "rank": idx,
                "confidence": candidate.get("score"),
                "status": "retrieved",
                "classification_basis": [
                    "Stage 1 shortlist via team CnCandidateRetriever."
                ],
                "classification_citations": [
                    {
                        "source_table": "cn_table/export_eu_retriever",
                        "source_id": cn8 or hs6,
                        "snippet": str(candidate.get("description") or "")[:500],
                        "reason": "Candidate retrieved by team export_eu CnCandidateRetriever.",
                    }
                ],
                "raw_team_row": candidate,
            }
        )
    return out


def _build_blackboard(
    *,
    row: dict[str, str],
    result: dict[str, Any],
    evidence: str,
    candidates: list[dict[str, Any]],
    team_candidates: list[dict[str, Any]],
    trace: dict[str, Any],
) -> dict[str, Any]:
    now = _now_iso()
    no = str(row.get("NO") or result.get("NO") or "0").zfill(3)
    product_id = "prod_001"
    candidate_set_id = "ccs_001"
    team_candidate_set_id = "ccs_team_001"
    product_evidence_state = {
        "object_type": "ProductEvidenceState",
        "created_by": "Evidence_Intake_Agent",
        "created_at": now,
        "product_id": product_id,
        "observed_facts": {
            "product_name": row.get("korean_name") or "",
            "description": row.get("product_input_for_rag") or "",
            "composition": [
                row.get("main_ingredients_original") or "",
                row.get("english_name_original") or "",
            ],
            "ocr_text": [],
            "coi_text": [],
            "source_urls": [],
            "origin_country": "KR",
            "intended_use": "food_import_classification_smoke",
            "raw_engine_evidence": evidence,
        },
        "inferred_facts": [],
        "unknowns": [],
        "evidence_pointers": [
            {
                "source_type": "csv_row",
                "source_path": str(DATASET_PATH),
                "row_id": row.get("NO"),
            }
        ],
    }
    candidate_code_set = {
        "object_type": "CandidateCodeSet",
        "created_by": "LLM_Classification_Engine",
        "created_at": now,
        "candidate_set_id": candidate_set_id,
        "product_id": product_id,
        "candidates": _candidate_objects(candidates),
        "llm_trace": trace,
    }
    team_candidate_code_set = {
        "object_type": "CandidateCodeSet",
        "created_by": "Team_Classification_Retriever",
        "created_at": now,
        "candidate_set_id": team_candidate_set_id,
        "product_id": product_id,
        "candidates": _team_candidate_objects(team_candidates),
    }
    evaluation = {
        "object_type": "ClassificationEvaluation",
        "created_by": "Evaluation_Harness",
        "created_at": now,
        "product_id": product_id,
        "candidate_set_id": candidate_set_id,
        "expected_existing_hs6": result.get("expected_existing_hs6"),
        "expected_asap_x_hs6": result.get("expected_asap_x_hs6"),
        "top1_hs6": result.get("top1_hs6"),
        "top5_hs6": result.get("top5_hs6"),
        "team_top1_hs6": result.get("team_top1_hs6"),
        "team_top5_hs6": result.get("team_top5_hs6"),
        "existing_top1_match": result.get("existing_top1_match"),
        "existing_top5_match": result.get("existing_top5_match"),
        "asap_x_top1_match": result.get("asap_x_top1_match"),
        "asap_x_top5_match": result.get("asap_x_top5_match"),
        "team_existing_top1_match": result.get("team_existing_top1_match"),
        "team_existing_top5_match": result.get("team_existing_top5_match"),
        "team_asap_x_top1_match": result.get("team_asap_x_top1_match"),
        "team_asap_x_top5_match": result.get("team_asap_x_top5_match"),
        "llm_parse_ok": result.get("llm_parse_ok"),
        "fallback_used": result.get("fallback_used"),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "team_elapsed_seconds": result.get("team_elapsed_seconds"),
        "error": result.get("error"),
        "team_error": result.get("team_error"),
    }
    agent_runs = [
        {
            "object_type": "AgentRun",
            "created_by": "Evidence_Intake_Agent",
            "created_at": now,
            "agent_run_id": "ar_001",
            "agent_name": "Evidence_Intake_Agent",
            "stage": "Evidence_Intake",
            "inputs_read": [f"dataset_row:{row.get('NO')}"],
            "outputs_written": [product_id],
            "reasoning_summary": "Converted generated food testset row into ProductEvidenceState.",
        },
        {
            "object_type": "AgentRun",
            "created_by": "LLM_Classification_Engine",
            "created_at": now,
            "agent_run_id": "ar_002",
            "agent_name": "LLM_Classification_Engine",
            "stage": "Classification",
            "inputs_read": [product_id],
            "outputs_written": [candidate_set_id],
            "ontology_reads": [
                {
                    "source_table": "cn_table",
                    "source_id": str(c.get("결정세번") or c.get("hs6") or ""),
                    "snippet": str(c.get("물품설명_원문") or "")[:240],
                    "reason": "Supabase cn_table candidate used by engine.classifier.",
                }
                for c in candidates[:TOP_K]
            ],
            "reasoning_summary": (
                f"LLM parse_ok={trace.get('parse_ok')} fallback={trace.get('fallback_used')} "
                f"top1={result.get('top1_hs6') or '-'}."
            ),
            "prompt_excerpt": str(trace.get("prompt") or "")[:2500],
            "llm_model": trace.get("model"),
        },
        {
            "object_type": "AgentRun",
            "created_by": "Team_Classification_Retriever",
            "created_at": now,
            "agent_run_id": "ar_003",
            "agent_name": "Team_Classification_Retriever",
            "stage": "Classification",
            "inputs_read": [product_id],
            "outputs_written": [team_candidate_set_id],
            "ontology_reads": [
                {
                    "source_table": "cn_table/export_eu_retriever",
                    "source_id": str(c.get("cn8") or c.get("hs6") or ""),
                    "snippet": str(c.get("description") or "")[:240],
                    "reason": "Team retriever shortlist candidate.",
                }
                for c in team_candidates[:TOP_K]
            ],
            "reasoning_summary": (
                f"Team retriever top1={result.get('team_top1_hs6') or '-'}; "
                "no LLM inference."
            ),
        },
        {
            "object_type": "AgentRun",
            "created_by": "Evaluation_Harness",
            "created_at": now,
            "agent_run_id": "ar_004",
            "agent_name": "Evaluation_Harness",
            "stage": "Evaluation",
            "inputs_read": [candidate_set_id, team_candidate_set_id],
            "outputs_written": ["eval_001"],
            "reasoning_summary": "Compared engine and team HS6 outputs against existing_hs6 and asap_x_hs6 references.",
        },
    ]
    return {
        "blackboard_id": f"generated_food_llm_{no}",
        "run_context": {
            "run_id": f"generated_food_llm_case_{no}",
            "created_at": now,
            "runtime_mode": "engine_llm_smoke",
            "dataset_path": str(DATASET_PATH),
            "row_no": row.get("NO"),
            "origin_country": "KR",
            "destination_market": "EU",
        },
        "product_evidence_state": product_evidence_state,
        "candidate_code_sets": [candidate_code_set, team_candidate_code_set],
        "evaluations": [evaluation],
        "agent_runs": agent_runs,
        "agent_outputs": {
            "Evidence_Intake_Agent": product_evidence_state,
            "LLM_Classification_Engine": candidate_code_set,
            "Team_Classification_Retriever": team_candidate_code_set,
            "Evaluation_Harness": evaluation,
        },
        "warnings": [],
        "errors": [result.get("error")] if result.get("error") else [],
    }


def _write_case_blackboard(
    *,
    row: dict[str, str],
    result: dict[str, Any],
    evidence: str,
    candidates: list[dict[str, Any]],
    team_candidates: list[dict[str, Any]],
    trace: dict[str, Any],
) -> str:
    BLACKBOARD_DIR.mkdir(parents=True, exist_ok=True)
    no = str(row.get("NO") or result.get("NO") or "0").zfill(3)
    path = BLACKBOARD_DIR / f"case_{no}_blackboard.json"
    payload = _build_blackboard(
        row=row,
        result=result,
        evidence=evidence,
        candidates=candidates,
        team_candidates=team_candidates,
        trace=trace,
    )
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _write_outputs(results: list[dict[str, Any]]) -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    expected_existing = [row for row in results if row.get("expected_existing_hs6")]
    expected_asap_x = [row for row in results if row.get("expected_asap_x_hs6")]
    payload = {
        "dataset_path": str(DATASET_PATH),
        "row_count": len(results),
        "top_k": TOP_K,
        "existing_hs6_scored": len(expected_existing),
        "existing_top1_accuracy": (
            sum(1 for row in expected_existing if row["existing_top1_match"]) / len(expected_existing)
            if expected_existing
            else None
        ),
        "existing_top5_accuracy": (
            sum(1 for row in expected_existing if row["existing_top5_match"]) / len(expected_existing)
            if expected_existing
            else None
        ),
        "asap_x_hs6_scored": len(expected_asap_x),
        "asap_x_top1_accuracy": (
            sum(1 for row in expected_asap_x if row["asap_x_top1_match"]) / len(expected_asap_x)
            if expected_asap_x
            else None
        ),
        "asap_x_top5_accuracy": (
            sum(1 for row in expected_asap_x if row["asap_x_top5_match"]) / len(expected_asap_x)
            if expected_asap_x
            else None
        ),
        "team_existing_top1_accuracy": (
            sum(1 for row in expected_existing if row["team_existing_top1_match"]) / len(expected_existing)
            if expected_existing
            else None
        ),
        "team_existing_top5_accuracy": (
            sum(1 for row in expected_existing if row["team_existing_top5_match"]) / len(expected_existing)
            if expected_existing
            else None
        ),
        "team_asap_x_top1_accuracy": (
            sum(1 for row in expected_asap_x if row["team_asap_x_top1_match"]) / len(expected_asap_x)
            if expected_asap_x
            else None
        ),
        "team_asap_x_top5_accuracy": (
            sum(1 for row in expected_asap_x if row["team_asap_x_top5_match"]) / len(expected_asap_x)
            if expected_asap_x
            else None
        ),
        "results": results,
    }
    json_path = ARTIFACT_DIR / "generated-food-llm-summary.json"
    csv_path = ARTIFACT_DIR / "generated-food-llm-results.csv"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "NO",
                "korean_name",
                "expected_existing_hs6",
                "expected_asap_x_hs6",
                "top1_hs6",
                "top5_hs6",
                "team_top1_hs6",
                "team_top5_hs6",
                "existing_top1_match",
                "existing_top5_match",
                "asap_x_top1_match",
                "asap_x_top5_match",
                "team_existing_top1_match",
                "team_existing_top5_match",
                "team_asap_x_top1_match",
                "team_asap_x_top5_match",
                "llm_parse_ok",
                "fallback_used",
                "blackboard_path",
                "elapsed_seconds",
                "team_elapsed_seconds",
                "error",
                "team_error",
            ],
        )
        writer.writeheader()
        for row in results:
            writer.writerow(
                {
                    "NO": row.get("NO"),
                    "korean_name": row.get("korean_name"),
                    "expected_existing_hs6": row.get("expected_existing_hs6"),
                    "expected_asap_x_hs6": row.get("expected_asap_x_hs6"),
                    "top1_hs6": row.get("top1_hs6"),
                    "top5_hs6": " ".join(row.get("top5_hs6") or []),
                    "team_top1_hs6": row.get("team_top1_hs6"),
                    "team_top5_hs6": " ".join(row.get("team_top5_hs6") or []),
                    "existing_top1_match": row.get("existing_top1_match"),
                    "existing_top5_match": row.get("existing_top5_match"),
                    "asap_x_top1_match": row.get("asap_x_top1_match"),
                    "asap_x_top5_match": row.get("asap_x_top5_match"),
                    "team_existing_top1_match": row.get("team_existing_top1_match"),
                    "team_existing_top5_match": row.get("team_existing_top5_match"),
                    "team_asap_x_top1_match": row.get("team_asap_x_top1_match"),
                    "team_asap_x_top5_match": row.get("team_asap_x_top5_match"),
                    "llm_parse_ok": row.get("llm_parse_ok"),
                    "fallback_used": row.get("fallback_used"),
                    "blackboard_path": row.get("blackboard_path"),
                    "elapsed_seconds": row.get("elapsed_seconds"),
                    "team_elapsed_seconds": row.get("team_elapsed_seconds"),
                    "error": row.get("error"),
                    "team_error": row.get("team_error"),
                }
            )


def main() -> int:
    from agents.llm_classifier import classify, get_last_trace

    rows = _load_rows()
    print(f"dataset={DATASET_PATH}")
    print(f"rows={len(rows)} top_k={TOP_K}")
    results: list[dict[str, Any]] = []

    for idx, row in enumerate(rows, 1):
        evidence = _build_evidence(row)
        expected_existing = _hs6(row.get("existing_hs6"))
        expected_asap_x = _hs6(row.get("asap_x_hs6"))
        started = time.monotonic()
        try:
            candidates = classify(evidence, top_k=TOP_K)
            trace = get_last_trace()
            error = ""
        except Exception as exc:  # noqa: BLE001
            candidates = []
            trace = {}
            error = str(exc)
        elapsed = round(time.monotonic() - started, 2)

        team_started = time.monotonic()
        try:
            team_candidates = _classify_team(row, evidence)
            team_error = ""
        except Exception as exc:  # noqa: BLE001
            team_candidates = []
            team_error = str(exc)
        team_elapsed = round(time.monotonic() - team_started, 2)

        top5 = _top_hs6(candidates)
        top1 = top5[0] if top5 else ""
        team_top5 = _top_hs6(team_candidates)
        team_top1 = team_top5[0] if team_top5 else ""
        result = {
            "NO": row.get("NO"),
            "master_code": row.get("master_code"),
            "korean_name": row.get("korean_name"),
            "english_name_original": row.get("english_name_original"),
            "expected_existing_hs6": expected_existing,
            "expected_asap_x_hs6": expected_asap_x,
            "existing_code_original": row.get("existing_code_original"),
            "asap_x_code_original": row.get("asap_x_code_original"),
            "evidence": evidence,
            "candidates": candidates,
            "top1_hs6": top1,
            "top5_hs6": top5,
            "team_candidates": team_candidates,
            "team_top1_hs6": team_top1,
            "team_top5_hs6": team_top5,
            "existing_top1_match": bool(expected_existing and top1 == expected_existing),
            "existing_top5_match": bool(expected_existing and expected_existing in top5),
            "asap_x_top1_match": bool(expected_asap_x and top1 == expected_asap_x),
            "asap_x_top5_match": bool(expected_asap_x and expected_asap_x in top5),
            "team_existing_top1_match": bool(expected_existing and team_top1 == expected_existing),
            "team_existing_top5_match": bool(expected_existing and expected_existing in team_top5),
            "team_asap_x_top1_match": bool(expected_asap_x and team_top1 == expected_asap_x),
            "team_asap_x_top5_match": bool(expected_asap_x and expected_asap_x in team_top5),
            "llm_trace": trace,
            "llm_parse_ok": trace.get("parse_ok"),
            "fallback_used": trace.get("fallback_used"),
            "elapsed_seconds": elapsed,
            "team_elapsed_seconds": team_elapsed,
            "error": error,
            "team_error": team_error,
        }
        result["blackboard_path"] = _write_case_blackboard(
            row=row,
            result=result,
            evidence=evidence,
            candidates=candidates,
            team_candidates=team_candidates,
            trace=trace,
        )
        results.append(result)
        _write_outputs(results)
        print(
            f"[{idx}/{len(rows)}] existing={expected_existing or '-'} asap_x={expected_asap_x or '-'} "
            f"llm={top1 or '-'} team={team_top1 or '-'} "
            f"llm_top5={' '.join(top5) or '-'} team_top5={' '.join(team_top5) or '-'} "
            f"parse={trace.get('parse_ok')} fallback={trace.get('fallback_used')} "
            f"| {row.get('korean_name', '')[:42]} (llm {elapsed}s/team {team_elapsed}s)"
            + (f" LLM_ERR={error}" if error else "")
            + (f" TEAM_ERR={team_error}" if team_error else ""),
            flush=True,
        )

    _write_outputs(results)
    print(f"\nSaved JSON: {ARTIFACT_DIR / 'generated-food-llm-summary.json'}")
    print(f"Saved CSV : {ARTIFACT_DIR / 'generated-food-llm-results.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

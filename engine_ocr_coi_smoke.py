"""Run the private engine classifier on saved OCR/COI evidence.

This script is intentionally engine-only.  It does not call the team
``export_eu`` classifier.  It accepts OCR result JSON shaped like the Kurly
pipeline outputs pasted during smoke testing, optionally appends matching COI
spreadsheet text, and records the LLM prompt/response trace emitted by
``engine.classifier``.

Environment:
  ASAP_OCR_RESULTS_JSON=/path/to/ocr_results.json   required
  ASAP_ENGINE_OCR_LIMIT=N                           optional first-N limit
  ASAP_ENGINE_OCR_TOP_K=N                           default 5
  ASAP_ENGINE_OCR_INCLUDE_COI=1                     default on
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
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

ANSWER_PATH = PROJECT_ROOT / "test" / "answer.csv"
ARTIFACT_DIR = PROJECT_ROOT / "artifacts" / "engine-ocr-coi"
TOP_K = int(os.environ.get("ASAP_ENGINE_OCR_TOP_K", "5") or "5")
LIMIT = int(os.environ.get("ASAP_ENGINE_OCR_LIMIT", "0") or "0")
INCLUDE_COI = os.environ.get("ASAP_ENGINE_OCR_INCLUDE_COI", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


def _norm_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _normalize_hs_code(value: object) -> str:
    hs = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(hs) == 9:
        hs = "0" + hs
    return hs


def _load_answers() -> dict[str, str]:
    answers: dict[str, str] = {}
    if not ANSWER_PATH.exists():
        return answers
    with ANSWER_PATH.open(newline="", encoding="utf-8-sig") as file:
        for row in csv.DictReader(file):
            url = _norm_url(row.get("상품 상세", ""))
            hs = _normalize_hs_code(row.get("미국 HS Code", ""))
            if url and len(hs) >= 6:
                answers[url] = hs[:6]
    return answers


def _as_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("results", "items", "products", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    return []


def _product_name(item: dict[str, Any]) -> str:
    obs = _observed_facts(item)
    product = item.get("product") if isinstance(item.get("product"), dict) else {}
    parsed = item.get("parsed_product_page") if isinstance(item.get("parsed_product_page"), dict) else {}
    return str(
        obs.get("product_name")
        or obs.get("name")
        or obs.get("title")
        or
        product.get("product_name")
        or parsed.get("product_name")
        or item.get("product_name")
        or item.get("heading")
        or item.get("title")
        or ""
    ).strip()


def _description(item: dict[str, Any]) -> str:
    obs = _observed_facts(item)
    product = item.get("product") if isinstance(item.get("product"), dict) else {}
    parsed = item.get("parsed_product_page") if isinstance(item.get("parsed_product_page"), dict) else {}
    return str(
        obs.get("description")
        or obs.get("short_description")
        or product.get("short_description")
        or parsed.get("short_description")
        or item.get("short_description")
        or item.get("description")
        or ""
    ).strip()


def _fact_texts(item: dict[str, Any]) -> list[str]:
    out: list[str] = []
    obs = _observed_facts(item)
    ocr_text = obs.get("ocr_text")
    if isinstance(ocr_text, list):
        out.extend(str(text).strip() for text in ocr_text if str(text).strip())
    elif isinstance(ocr_text, str) and ocr_text.strip():
        out.append(ocr_text.strip())
    composition = obs.get("composition")
    if isinstance(composition, list):
        out.extend(str(text).strip() for text in composition if str(text).strip())
    elif isinstance(composition, str) and composition.strip():
        out.append(composition.strip())

    ocr_summary = item.get("ocr_summary") if isinstance(item.get("ocr_summary"), dict) else {}
    normalization = ocr_summary.get("normalization") if isinstance(ocr_summary.get("normalization"), dict) else {}
    fact_texts = normalization.get("fact_texts")
    if isinstance(fact_texts, list):
        out.extend(str(text).strip() for text in fact_texts if str(text).strip())
    combined = item.get("combined_ocr_text")
    if isinstance(combined, str) and combined.strip():
        out.insert(0, combined.strip())
    ocr = item.get("ocr") if isinstance(item.get("ocr"), dict) else {}
    preview = ocr.get("combined_text_preview")
    if isinstance(preview, str) and preview.strip():
        out.append(preview.strip())
    return out


def _observed_facts(item: dict[str, Any]) -> dict[str, Any]:
    pes = item.get("product_evidence_state")
    if isinstance(pes, dict) and isinstance(pes.get("observed_facts"), dict):
        return pes["observed_facts"]
    if isinstance(item.get("observed_facts"), dict):
        return item["observed_facts"]
    return {}


def _build_engine_input(item: dict[str, Any]) -> str:
    parts = [
        f"product_name: {_product_name(item)}",
        f"description: {_description(item)}",
    ]
    facts = _fact_texts(item)
    if facts:
        parts.append("OCR evidence:")
        parts.extend(facts)
    else:
        parts.append("OCR evidence: [missing]")
    return "\n".join(part for part in parts if part).strip()


def _append_coi(row: dict[str, Any], evidence: str) -> tuple[str, str, int]:
    if not INCLUDE_COI:
        return evidence, "", 0
    from agents.coi_loader import load_coi_evidence

    case_index = row.get("index")
    try:
        case_index_int = int(case_index) if case_index is not None else None
    except (TypeError, ValueError):
        case_index_int = None
    coi = load_coi_evidence(
        case_index=case_index_int,
        product_name=_product_name(row),
    )
    if coi is None:
        return evidence, "", 0
    merged = "\n".join([evidence, f"COI evidence from {coi.path.name}:", coi.text])
    return merged, str(coi.path), len(coi.text)


def _load_ocr_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = _as_list(payload)
    if LIMIT > 0:
        rows = rows[:LIMIT]
    return rows


def _top_hs6(candidates: list[dict[str, Any]]) -> list[str]:
    return [
        hs6
        for hs6 in (str(candidate.get("hs6") or "")[:6] for candidate in candidates)
        if len(hs6) == 6
    ]


def _write_outputs(payload: dict[str, Any]) -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = ARTIFACT_DIR / "engine-ocr-coi-summary.json"
    csv_path = ARTIFACT_DIR / "engine-ocr-coi-results.csv"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "index",
                "product_name",
                "url",
                "expected_hs6",
                "top1_hs6",
                "top5_hs6",
                "top1_match",
                "top5_match",
                "coi_path",
                "coi_text_length",
                "llm_parse_ok",
                "fallback_used",
                "elapsed_seconds",
                "error",
            ],
        )
        writer.writeheader()
        for row in payload["results"]:
            writer.writerow(
                {
                    "index": row.get("index"),
                    "product_name": row.get("product_name"),
                    "url": row.get("url"),
                    "expected_hs6": row.get("expected_hs6"),
                    "top1_hs6": row.get("top1_hs6"),
                    "top5_hs6": " ".join(row.get("top5_hs6") or []),
                    "top1_match": row.get("top1_match"),
                    "top5_match": row.get("top5_match"),
                    "coi_path": row.get("coi_path"),
                    "coi_text_length": row.get("coi_text_length"),
                    "llm_parse_ok": row.get("llm_parse_ok"),
                    "fallback_used": row.get("fallback_used"),
                    "elapsed_seconds": row.get("elapsed_seconds"),
                    "error": row.get("error"),
                }
            )

    print(f"\nSaved JSON: {json_path}")
    print(f"Saved CSV : {csv_path}")


def main() -> int:
    ocr_path_text = os.environ.get("ASAP_OCR_RESULTS_JSON") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not ocr_path_text:
        print("Set ASAP_OCR_RESULTS_JSON=/path/to/ocr_results.json or pass the path as argv[1].")
        return 2
    ocr_path = Path(ocr_path_text).expanduser().resolve()
    if not ocr_path.exists():
        print(f"OCR JSON not found: {ocr_path}")
        return 2

    from agents.llm_classifier import classify, get_last_trace

    answers = _load_answers()
    rows = _load_ocr_rows(ocr_path)
    print(f"ocr rows={len(rows)} top_k={TOP_K} include_coi={INCLUDE_COI} source={ocr_path}")

    results: list[dict[str, Any]] = []
    for ordinal, row in enumerate(rows, 1):
        obs = _observed_facts(row)
        source_urls = obs.get("source_urls") if isinstance(obs.get("source_urls"), list) else []
        url = _norm_url(str(row.get("product_page_url") or row.get("url") or (source_urls[0] if source_urls else "")))
        expected = answers.get(url, "")
        evidence = _build_engine_input(row)
        evidence, coi_path, coi_text_length = _append_coi(row, evidence)
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

        top5 = _top_hs6(candidates)
        top1 = top5[0] if top5 else ""
        result = {
            "index": row.get("index"),
            "url": url,
            "product_name": _product_name(row),
            "expected_hs6": expected,
            "candidates": candidates,
            "top1_hs6": top1,
            "top5_hs6": top5,
            "top1_match": bool(expected and top1 == expected),
            "top5_match": bool(expected and expected in top5),
            "evidence": evidence,
            "coi_path": coi_path,
            "coi_text_length": coi_text_length,
            "llm_trace": trace,
            "llm_parse_ok": trace.get("parse_ok"),
            "fallback_used": trace.get("fallback_used"),
            "elapsed_seconds": elapsed,
            "error": error,
        }
        results.append(result)
        print(
            f"[{ordinal}/{len(rows)}] expected={expected or '-'} top1={top1 or '-'} "
            f"top5={' '.join(top5) or '-'} parse={trace.get('parse_ok')} "
            f"fallback={trace.get('fallback_used')} | {result['product_name'][:45]} ({elapsed}s)"
            + (f" ERR={error}" if error else ""),
            flush=True,
        )

    expected_rows = [row for row in results if row.get("expected_hs6")]
    summary = {
        "input_path": str(ocr_path),
        "row_count": len(results),
        "expected_row_count": len(expected_rows),
        "top1_accuracy": (
            sum(1 for row in expected_rows if row["top1_match"]) / len(expected_rows)
            if expected_rows
            else None
        ),
        "top5_accuracy": (
            sum(1 for row in expected_rows if row["top5_match"]) / len(expected_rows)
            if expected_rows
            else None
        ),
        "results": results,
    }
    _write_outputs(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

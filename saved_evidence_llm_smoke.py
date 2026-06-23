"""Run the LLM classifier against saved KurlyGlobal evidence.

This bypasses live URL collection.  It uses:

* artifacts/kurlyglobal-smoke/render-smoke-summary.json for saved heading/title
* /Users/snu/ASAP/test/answer.csv for HS6 reference labels
* engine.classifier.classify for the current LLM-based classifier

Environment:
  ASAP_SAVED_EVIDENCE_LIMIT=N   optional first-N limit
  ASAP_SAVED_EVIDENCE_INCLUDE_COI=1   append matched COI xlsx text
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

RENDER_SUMMARY_PATH = PROJECT_ROOT / "artifacts" / "kurlyglobal-smoke" / "render-smoke-summary.json"
ANSWER_PATH = Path("/Users/snu/ASAP/test/answer.csv")
ARTIFACT_DIR = PROJECT_ROOT / "artifacts" / "classifier-ab"
TOP_K = int(os.environ.get("ASAP_SAVED_EVIDENCE_TOP_K", "5") or "5")
INCLUDE_COI = os.environ.get("ASAP_SAVED_EVIDENCE_INCLUDE_COI", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _norm_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _load_answers() -> dict[str, str]:
    answers: dict[str, str] = {}
    with ANSWER_PATH.open(newline="", encoding="utf-8-sig") as file:
        for row in csv.DictReader(file):
            url = _norm_url(row.get("상품 상세", ""))
            hs = _normalize_hs_code(row.get("미국 HS Code", ""))
            if url and len(hs) >= 6:
                answers[url] = hs[:6]
    return answers


def _normalize_hs_code(value: object) -> str:
    hs = "".join(ch for ch in str(value or "") if ch.isdigit())
    # Spreadsheet tools often coerce leading-zero HS codes to numbers.  Food
    # chapter 07 examples such as 0710807060 can appear as 710807060.
    if len(hs) == 9:
        hs = "0" + hs
    return hs


def _load_saved_evidence() -> list[dict[str, Any]]:
    data = json.loads(RENDER_SUMMARY_PATH.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for item in data.get("results", []):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        heading = str(item.get("heading") or "").strip()
        title = str(item.get("title") or "").strip()
        evidence = "\n".join(
            part
            for part in [
                f"product_name: {heading}" if heading else "",
                f"page_title: {title}" if title and title != heading else "",
            ]
            if part
        )
        rows.append(
            {
                "index": item.get("index"),
                "url": url,
                "normalized_url": _norm_url(url),
                "heading": heading,
                "title": title,
                "evidence": evidence,
            }
        )
    return rows


def _append_coi_evidence(row: dict[str, Any]) -> dict[str, Any]:
    if not INCLUDE_COI:
        return row
    from agents.coi_loader import load_coi_evidence

    coi = load_coi_evidence(
        case_index=int(row["index"]) if str(row.get("index") or "").isdigit() else None,
        product_name=str(row.get("heading") or row.get("title") or ""),
    )
    if coi is None:
        row["coi_path"] = ""
        row["coi_text_length"] = 0
        return row
    row = dict(row)
    row["coi_path"] = str(coi.path)
    row["coi_text_length"] = len(coi.text)
    row["evidence"] = "\n".join(
        part
        for part in [
            row.get("evidence") or "",
            f"COI evidence from {coi.path.name}:",
            coi.text,
        ]
        if part
    )
    return row


def _write_outputs(payload: dict[str, Any]) -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = ARTIFACT_DIR / "saved-evidence-llm-summary.json"
    csv_path = ARTIFACT_DIR / "saved-evidence-llm-results.csv"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "index",
                "url",
                "heading",
                "expected_hs6",
                "top1_hs6",
                "top5_hs6",
                "top1_match",
                "in_top5",
                "coi_path",
                "coi_text_length",
                "elapsed_seconds",
                "error",
            ],
        )
        writer.writeheader()
        for row in payload["results"]:
            writer.writerow(
                {
                    "index": row["index"],
                    "url": row["url"],
                    "heading": row["heading"],
                    "expected_hs6": row["expected_hs6"],
                    "top1_hs6": row["top1_hs6"],
                    "top5_hs6": " ".join(row["top5_hs6"]),
                    "top1_match": row["top1_match"],
                    "in_top5": row["in_top5"],
                    "coi_path": row.get("coi_path", ""),
                    "coi_text_length": row.get("coi_text_length", 0),
                    "elapsed_seconds": row["elapsed_seconds"],
                    "error": row["error"],
                }
            )

    print(f"\nSaved JSON: {json_path}")
    print(f"Saved CSV : {csv_path}")


def main() -> int:
    from agents.llm_classifier import classify

    answers = _load_answers()
    rows = [
        _append_coi_evidence(row)
        for row in _load_saved_evidence()
        if row["normalized_url"] in answers and row["evidence"]
    ]
    limit = int(os.environ.get("ASAP_SAVED_EVIDENCE_LIMIT", "0") or "0")
    if limit > 0:
        rows = rows[:limit]

    print(f"saved evidence rows={len(rows)} top_k={TOP_K}")
    results: list[dict[str, Any]] = []

    for ordinal, row in enumerate(rows, 1):
        started = time.monotonic()
        expected = answers[row["normalized_url"]]
        try:
            candidates = classify(row["evidence"], top_k=TOP_K)
            error = None
        except Exception as exc:  # noqa: BLE001
            candidates = []
            error = str(exc)
        elapsed = round(time.monotonic() - started, 2)

        top5 = [str(c.get("hs6") or "")[:6] for c in candidates if str(c.get("hs6") or "")[:6]]
        top1 = top5[0] if top5 else ""
        top1_match = bool(top1) and top1 == expected
        in_top5 = expected in top5
        print(
            f"[{ordinal}/{len(rows)}] expected={expected} top1={top1 or '-'} "
            f"top5={' '.join(top5) or '-'} | {row['heading'][:45]} ({elapsed}s)"
            + (f" ERR={error}" if error else ""),
            flush=True,
        )
        results.append(
            {
                "index": row["index"],
                "url": row["url"],
                "heading": row["heading"],
                "title": row["title"],
                "evidence": row["evidence"],
                "expected_hs6": expected,
                "candidates": candidates,
                "top1_hs6": top1,
                "top5_hs6": top5,
                "top1_match": top1_match,
                "in_top5": in_top5,
                "elapsed_seconds": elapsed,
                "error": error,
            }
        )

    total = len(results)
    top1_count = sum(1 for row in results if row["top1_match"])
    top5_count = sum(1 for row in results if row["in_top5"])
    error_count = sum(1 for row in results if row["error"])
    payload = {
        "mode": "saved_kurlyglobal_heading_title_to_llm",
        "render_summary_path": str(RENDER_SUMMARY_PATH),
        "answer_path": str(ANSWER_PATH),
        "include_coi": INCLUDE_COI,
        "total": total,
        "top_k": TOP_K,
        "top1": top1_count,
        "top5": top5_count,
        "top1_accuracy": round(top1_count / total, 4) if total else 0.0,
        "top5_accuracy": round(top5_count / total, 4) if total else 0.0,
        "errors": error_count,
        "results": results,
    }
    print(
        f"\nSUMMARY total={total} top1={top1_count}/{total} "
        f"({payload['top1_accuracy'] * 100:.1f}%) "
        f"top5={top5_count}/{total} ({payload['top5_accuracy'] * 100:.1f}%) "
        f"errors={error_count}"
    )
    _write_outputs(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

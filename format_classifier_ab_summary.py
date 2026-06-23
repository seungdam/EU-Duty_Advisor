"""Format classifier A/B smoke artifacts into a compact answer/prediction view.

Inputs:
  artifacts/classifier-ab/ab-llm-summary.json
  artifacts/classifier-ab/ab-retriever-summary.json
  test/answer.csv

Outputs:
  artifacts/classifier-ab/ab-readable-summary.csv
  artifacts/classifier-ab/ab-readable-summary.md
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
ANSWER_PATH = PROJECT_ROOT / "test" / "answer.csv"
ARTIFACT_DIR = PROJECT_ROOT / "artifacts" / "classifier-ab"
LLM_PATH = ARTIFACT_DIR / "ab-llm-summary.json"
TEAM_PATH = ARTIFACT_DIR / "ab-retriever-summary.json"
OUT_CSV = ARTIFACT_DIR / "ab-readable-summary.csv"
OUT_MD = ARTIFACT_DIR / "ab-readable-summary.md"


def hs6(raw: str) -> str:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if 0 < len(digits) < 10:
        digits = digits.zfill(10)
    return digits[:6] if len(digits) >= 6 else ""


def load_answer_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with ANSWER_PATH.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader, 1):
            url = (row.get("상품 상세") or "").strip()
            raw = (row.get("미국 HS Code") or "").strip()
            if not url:
                continue
            rows.append({"index": str(idx), "url": url, "answer_raw": raw, "answer_hs6": hs6(raw)})
    return rows


def load_summary(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for item in data.get("results") or []:
        url = str(item.get("url") or "").strip()
        if url:
            out[url] = item
    return out


def top1(item: dict[str, Any] | None) -> str:
    if not item:
        return ""
    values = item.get("our_hs6") or []
    if values:
        return str(values[0] or "")
    candidates = item.get("candidates") or []
    if candidates:
        return str(candidates[0].get("hs6") or "")
    return ""


def top5(item: dict[str, Any] | None) -> str:
    if not item:
        return ""
    values = item.get("our_hs6") or []
    if values:
        return " ".join(str(v or "") for v in values[:5] if v)
    candidates = item.get("candidates") or []
    return " ".join(str(c.get("hs6") or "") for c in candidates[:5] if c.get("hs6"))


def product_name(item: dict[str, Any] | None) -> str:
    return str((item or {}).get("product_name") or "")


def mark(pred: str, answer: str) -> str:
    if not pred:
        return "-"
    return "O" if pred == answer else "X"


def chunked(items: list[dict[str, str]], size: int = 12) -> list[list[dict[str, str]]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def main() -> int:
    answers = load_answer_rows()
    llm = load_summary(LLM_PATH)
    team = load_summary(TEAM_PATH)

    records: list[dict[str, str]] = []
    for row in answers:
        url = row["url"]
        llm_item = llm.get(url)
        team_item = team.get(url)
        llm_top1 = top1(llm_item)
        team_top1 = top1(team_item)
        records.append({
            "index": row["index"],
            "url": url,
            "product_name": product_name(llm_item) or product_name(team_item),
            "answer_raw": row["answer_raw"],
            "answer_hs6": row["answer_hs6"],
            "llm_top1": llm_top1,
            "llm_top5": top5(llm_item),
            "llm_match": mark(llm_top1, row["answer_hs6"]),
            "team_top1": team_top1,
            "team_top5": top5(team_item),
            "team_match": mark(team_top1, row["answer_hs6"]),
        })

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["row"] + [r["index"] for r in records])
        writer.writerow(["정답_HS6"] + [r["answer_hs6"] for r in records])
        writer.writerow(["LLM_top1"] + [r["llm_top1"] for r in records])
        writer.writerow(["LLM_match"] + [r["llm_match"] for r in records])
        writer.writerow(["팀장_top1"] + [r["team_top1"] for r in records])
        writer.writerow(["팀장_match"] + [r["team_match"] for r in records])
        writer.writerow([])
        detail_fields = [
            "index",
            "product_name",
            "answer_raw",
            "answer_hs6",
            "llm_top1",
            "llm_top5",
            "llm_match",
            "team_top1",
            "team_top5",
            "team_match",
            "url",
        ]
        writer.writerow(detail_fields)
        for r in records:
            writer.writerow([r[field] for field in detail_fields])

    llm_scored = [r for r in records if r["llm_top1"]]
    team_scored = [r for r in records if r["team_top1"]]
    llm_ok = sum(1 for r in records if r["llm_match"] == "O")
    team_ok = sum(1 for r in records if r["team_match"] == "O")
    lines = [
        "# Classifier A/B Readable Summary",
        "",
        f"- answer rows: {len(records)}",
        f"- LLM top1: {llm_ok}/{len(records)} ({llm_ok / len(records) * 100:.1f}%)",
        f"- team top1: {team_ok}/{len(records)} ({team_ok / len(records) * 100:.1f}%)",
        f"- LLM rows with output: {len(llm_scored)}",
        f"- team rows with output: {len(team_scored)}",
        "",
        "## Matrix",
        "",
    ]
    for group in chunked(records):
        headers = ["row"] + [r["index"] for r in group]
        sep = ["---"] * len(headers)
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join(sep) + " |")
        lines.append("| 정답_HS6 | " + " | ".join(r["answer_hs6"] for r in group) + " |")
        lines.append("| LLM_top1 | " + " | ".join(r["llm_top1"] or "-" for r in group) + " |")
        lines.append("| LLM_match | " + " | ".join(r["llm_match"] for r in group) + " |")
        lines.append("| 팀장_top1 | " + " | ".join(r["team_top1"] or "-" for r in group) + " |")
        lines.append("| 팀장_match | " + " | ".join(r["team_match"] for r in group) + " |")
        lines.append("")

    lines.extend(["## Detail", ""])
    lines.append("| # | product | answer | LLM top1/top5 | team top1/top5 |")
    lines.append("|---:|---|---|---|---|")
    for r in records:
        product = r["product_name"].replace("|", "/")[:36]
        lines.append(
            f"| {r['index']} | {product} | {r['answer_hs6']} | "
            f"{r['llm_top1'] or '-'} ({r['llm_match']}) / {r['llm_top5'] or '-'} | "
            f"{r['team_top1'] or '-'} ({r['team_match']}) / {r['team_top5'] or '-'} |"
        )
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Saved CSV: {OUT_CSV}")
    print(f"Saved MD : {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

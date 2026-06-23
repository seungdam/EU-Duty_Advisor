"""
evidence_coverage_score — DB 기반 결정적 점수 (LLM 없음)

점수 구성 (합계 최대 1.00):
  BTI support           최대 0.35
  CELEX evidence        최대 0.35
  CN explanatory note   최대 0.20
  data quality bonus    최대 0.10

사용:
  from agents.evidence_coverage import score_hs6
  result = score_hs6("190230")
  result = score_hs6("1902.30")   # 점·공백 자동 정규화
"""
import math
import re
import psycopg2

DB_NAME = "asap_db_v1"


# ── CELEX 본문 위치 추출 ───────────────────────────────────────
def extract_celex_passage(full_text: str, hs6: str, max_passages: int = 3) -> list[dict]:
    """
    CELEX full_text에서 HS6 관련 조항 위치와 텍스트를 추출.
    특이도 높은 순(hs6 직접 언급 > heading > chapter)으로 정렬.
    """
    if not full_text:
        return []

    heading  = hs6[:4]                           # 1902
    chapter  = hs6[:2].lstrip('0') or '0'        # 19
    h_dot    = f"{heading[:2]}.{heading[2:]}"    # 19.02

    # 특이도 순 검색 패턴 (index = 우선순위)
    patterns = [
        (hs6,                      0, 'HS6 직접'),
        (f"Ex{hs6}",               0, 'HS6 예외'),
        (f"heading {heading}",     1, '호 참조'),
        (f"subheading {hs6}",      1, '소호 참조'),
        (h_dot,                    1, '호 점표기'),
        (heading,                  2, '호 번호'),
        (f"Chapter {chapter}",     3, '류 참조'),
        (f"chapter {chapter}",     3, '류 참조'),
        (f"CN code {hs6}",         0, 'CN코드'),
        (f"CN {heading}",          1, 'CN호'),
    ]

    found     = []
    seen_pos  = []

    for term, priority, label in patterns:
        search = full_text.lower()
        start  = 0
        while True:
            idx = search.find(term.lower(), start)
            if idx == -1:
                break
            # 이미 수집된 위치와 100자 이내 중복 제거
            if any(abs(idx - p) < 100 for p in seen_pos):
                start = idx + 1
                continue

            # 단락 경계 찾기
            p_start = full_text.rfind('\n', max(0, idx - 300), idx)
            p_start = p_start + 1 if p_start != -1 else max(0, idx - 200)
            p_end   = full_text.find('\n\n', idx)
            p_end   = p_end if p_end != -1 and p_end < idx + 600 else idx + 400

            snippet = full_text[p_start:p_end].strip()
            # 공백 정리
            snippet = re.sub(r' {2,}', ' ', snippet)

            if len(snippet) >= 30:
                found.append({
                    'priority':    priority,
                    'match_term':  term,
                    'match_label': label,
                    'char_pos':    idx,
                    'passage':     snippet[:500],
                })
                seen_pos.append(idx)

            start = idx + 1
            if len(found) >= max_passages * 4:
                break

    # 우선순위 → 위치 순 정렬, 상위 max_passages 반환
    found.sort(key=lambda x: (x['priority'], x['char_pos']))
    return found[:max_passages]


def fetch_celex_with_passage(hs6: str) -> list[dict]:
    """
    fetch_celex_evidence + 각 법령 본문에서 관련 조항 추출 포함 버전.
    """
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (e.celex_id, e.evidence_type)
                   e.evidence_type, e.evidence_weight,
                   e.needs_review, e.match_status,
                   e.celex_id, e.evidence_label,
                   c.title_en, c.domain, c.reg_category,
                   c.document_quality, c.in_force, c.full_text
            FROM hscode_celex_evidence e
            JOIN celex_regulations c ON e.celex_reg_id = c.id
            WHERE e.hs6 = %s
              AND c.document_quality IN ('ok', 'truncated')
            ORDER BY e.celex_id, e.evidence_type, e.evidence_weight DESC
        """, (hs6,))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    result = []
    for row in sorted(rows, key=lambda x: float(x['evidence_weight']), reverse=True):
        passages = extract_celex_passage(row['full_text'] or '', hs6)
        row['passages'] = passages
        row.pop('full_text')  # 전문 제거 (메모리 절약)
        result.append(row)

    return result

# CELEX evidence_weight 기준 강도 분류
STRONG_EVIDENCE_TYPES = {
    "veterinary_control", "feed_food_restriction", "gmo_control",
    "iuu_fishing_control", "fluorinated_gas_control",
    "food_safety", "cites_control",
    "cosmetic_regulation", "import_control",
    "labelling", "organic_control",
}
WEAK_EVIDENCE_TYPES = {
    "tariff_context", "origin_fta_context", "commercial_doc_context",
    "general_context", "a2m_context", "trade_defense_context",
}


# ── 정규화 ─────────────────────────────────────────────────────
def normalize_hs(code: str) -> dict:
    """입력 코드 → hs6 / cn8 / taric_code"""
    raw = code.replace(".", "").replace(" ", "").strip()
    return {
        "hs6":       raw[:6]  if len(raw) >= 6 else raw.ljust(6, "0"),
        "cn8":       raw[:8]  if len(raw) >= 8 else None,
        "taric_code": raw[:10] if len(raw) >= 10 else None,
    }


# ── DB 조회 ───────────────────────────────────────────────────
def _conn():
    return psycopg2.connect(dbname=DB_NAME)


def fetch_bti(hs6: str) -> list[dict]:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT bti_reference, issuing_country, cn_code,
                   valid_from, valid_to_date,
                   description_of_goods, classification_justification
            FROM eu_bti
            WHERE hs6 = %s
              AND (valid_to_date IS NULL OR valid_to_date >= CURRENT_DATE)
            ORDER BY valid_from DESC
            LIMIT 20
        """, (hs6,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def fetch_celex_evidence(hs6: str) -> list[dict]:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (e.celex_id, e.evidence_type)
                   e.evidence_type, e.evidence_weight,
                   e.needs_review, e.match_status,
                   e.celex_id, e.evidence_label,
                   c.title_en, c.domain, c.reg_category,
                   c.document_quality, c.in_force
            FROM hscode_celex_evidence e
            JOIN celex_regulations c ON e.celex_reg_id = c.id
            WHERE e.hs6 = %s
              AND c.document_quality IN ('ok', 'truncated')
            ORDER BY e.celex_id, e.evidence_type, e.evidence_weight DESC, e.needs_review ASC
        """, (hs6,))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        # 최종 정렬: evidence_weight DESC
        return sorted(rows, key=lambda x: float(x["evidence_weight"]), reverse=True)


def fetch_cn_notes(hs6: str) -> dict:
    heading = hs6[:4]
    chapter = hs6[:2]
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT hs_heading, hs_chapter, section_title,
                   heading_text, notes
            FROM cn_explanatory_notes
            WHERE hs_heading = %s OR hs_chapter = %s
            ORDER BY CASE WHEN hs_heading = %s THEN 0 ELSE 1 END
            LIMIT 5
        """, (heading, chapter, heading))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {
        "heading_match": any(r["hs_heading"] == heading for r in rows),
        "chapter_match": any(r["hs_chapter"] == chapter for r in rows),
        "rows": rows,
    }


# ── 점수 계산 ─────────────────────────────────────────────────
def _bti_score(bti_rows: list[dict]) -> tuple[float, dict]:
    n = len(bti_rows)
    # log 스케일: 1건→0.12, 5건→0.25, 10건→0.35
    raw = math.log1p(n) / math.log1p(10) * 0.35
    score = round(min(0.35, raw), 4)
    return score, {"count": n, "countries": list({r["issuing_country"] for r in bti_rows})}


def _celex_score(celex_rows: list[dict]) -> tuple[float, dict]:
    if not celex_rows:
        return 0.0, {"count": 0, "types": []}

    seen_types = set()
    contribution = 0.0

    for row in celex_rows:
        etype  = row["evidence_type"]
        weight = float(row["evidence_weight"])
        review = row["needs_review"]
        dq     = row["document_quality"]

        if etype in seen_types:
            continue
        seen_types.add(etype)

        # needs_review=True(chapter rule 추정)는 가중치 절반
        effective = weight * (0.5 if review else 1.0)
        # truncated 문서는 소폭 감산
        if dq == "truncated":
            effective *= 0.9

        contribution += effective

    # 강한 근거 유형 보유 여부 보너스
    has_strong = bool(seen_types & STRONG_EVIDENCE_TYPES)
    cap = 0.35
    raw = contribution / max(1, len(seen_types)) * (1.2 if has_strong else 0.8)
    score = round(min(cap, raw), 4)

    return score, {
        "count": len(celex_rows),
        "distinct_types": len(seen_types),
        "types": sorted(seen_types),
        "has_strong": has_strong,
    }


def _cn_score(cn: dict) -> tuple[float, dict]:
    if cn["heading_match"]:
        return 0.20, {"match": "heading"}
    elif cn["chapter_match"]:
        return 0.10, {"match": "chapter"}
    return 0.00, {"match": "none"}


def _quality_score(bti_rows: list[dict], celex_rows: list[dict]) -> tuple[float, dict]:
    score = 0.0
    notes = []

    # 유효 BTI 존재
    active_bti = [r for r in bti_rows if r["valid_to_date"] is None or
                  str(r["valid_to_date"]) >= "2026-01-01"]
    if active_bti:
        score += 0.05
        notes.append(f"active_bti={len(active_bti)}")

    # CELEX 전부 'ok' (truncated 없음)
    if celex_rows and all(r["document_quality"] == "ok" for r in celex_rows):
        score += 0.05
        notes.append("all_celex_ok")
    elif celex_rows:
        score += 0.02
        notes.append("some_celex_truncated")

    return round(min(0.10, score), 4), {"notes": notes}


# ── 메인 ─────────────────────────────────────────────────────
def score_hs6(candidate_code: str, verbose: bool = True) -> dict:
    norm  = normalize_hs(candidate_code)
    hs6   = norm["hs6"]

    bti_rows   = fetch_bti(hs6)
    celex_rows = fetch_celex_evidence(hs6)
    cn         = fetch_cn_notes(hs6)

    bti_score,   bti_detail   = _bti_score(bti_rows)
    celex_score, celex_detail = _celex_score(celex_rows)
    cn_score,    cn_detail    = _cn_score(cn)
    qual_score,  qual_detail  = _quality_score(bti_rows, celex_rows)

    total = round(bti_score + celex_score + cn_score + qual_score, 4)

    result = {
        "hs6":                    hs6,
        "evidence_coverage_score": total,
        "breakdown": {
            "bti_support":   {"score": bti_score,   "max": 0.35, **bti_detail},
            "celex_evidence":{"score": celex_score,  "max": 0.35, **celex_detail},
            "cn_note":       {"score": cn_score,     "max": 0.20, **cn_detail},
            "data_quality":  {"score": qual_score,   "max": 0.10, **qual_detail},
        },
        "raw": {
            "bti_cases":    bti_rows[:5],
            "celex_top5":   celex_rows[:5],
            "cn_note_rows": cn["rows"][:2],
        }
    }

    if verbose:
        _print_trace(result)

    return result


def _print_trace(r: dict):
    b     = r["breakdown"]
    total = r["evidence_coverage_score"]
    grade = "HIGH" if total >= 0.70 else "MED" if total >= 0.40 else "LOW"
    W     = 72

    # ── 점수 요약 ─────────────────────────────────────────────
    print(f"\n{'='*W}")
    print(f"HS6: {r['hs6']}  |  evidence_coverage_score: {total:.4f}  [{grade}]")
    print(f"{'─'*W}")
    print(f"  {'항목':<22} {'점수':>7}  {'최대':>5}  상세")
    print(f"  {'─'*22} {'─'*7}  {'─'*5}  {'─'*24}")
    print(f"  {'BTI support':<22} {b['bti_support']['score']:>7.4f}  {'0.35':>5}"
          f"  cases={b['bti_support']['count']} | {b['bti_support']['countries']}")
    print(f"  {'CELEX evidence':<22} {b['celex_evidence']['score']:>7.4f}  {'0.35':>5}"
          f"  rows={b['celex_evidence']['count']} | types={b['celex_evidence']['distinct_types']}"
          f" | strong={b['celex_evidence']['has_strong']}")
    print(f"  {'CN explanatory note':<22} {b['cn_note']['score']:>7.4f}  {'0.20':>5}"
          f"  match={b['cn_note']['match']}")
    print(f"  {'Data quality bonus':<22} {b['data_quality']['score']:>7.4f}  {'0.10':>5}"
          f"  {', '.join(b['data_quality']['notes']) or 'none'}")
    print(f"  {'─'*22} {'─'*7}")
    print(f"  {'TOTAL':<22} {total:>7.4f}")

    # ── BTI 상세 ─────────────────────────────────────────────
    bti = r["raw"]["bti_cases"]
    if bti:
        print(f"\n{'─'*W}")
        print(f"  [BTI 결정사례]  ({len(bti)}건 조회, 상위 5건 표시)")
        print(f"{'─'*W}")
        for i, c in enumerate(bti[:5], 1):
            ref      = c.get("bti_reference", "")
            country  = c.get("issuing_country", "")
            cn_code  = c.get("cn_code", "")
            vfrom    = c.get("valid_from", "")
            vto      = c.get("valid_to_date") or "현재"
            desc_raw = (c.get("description_of_goods") or "").strip()
            just_raw = (c.get("classification_justification") or "").strip()
            kw       = (c.get("keywords") or "").strip()

            # 기준년도 파싱 (valid_from 또는 참조번호에서)
            year = vfrom[-4:] if vfrom and len(vfrom) >= 4 else "—"

            print(f"\n  [{i}]")
            print(f"    기준년도       : {year}")
            print(f"    참조번호       : {ref}")
            print(f"    이슈장소       : {country}")
            print(f"    시행일자       : {vfrom} ~ {vto}")
            print(f"    결정세번       : {cn_code}")
            print(f"    물품설명_원문  : {desc_raw[:120]}")
            print(f"    물품설명_영문  : {'(번역 미적용)' if desc_raw else '—'}")
            print(f"    물품설명_한글  : {'(번역 미적용)' if desc_raw else '—'}")
            print(f"    분류사유_원문  : {just_raw[:150]}")
            print(f"    분류사유_한글  : {'(번역 미적용)' if just_raw else '—'}")
            print(f"    키워드_원문    : {kw[:100] if kw else '—'}")

    # ── CELEX 상세 + 조항 위치 ───────────────────────────────
    celex_full = fetch_celex_with_passage(r['hs6'])
    if celex_full:
        print(f"\n{'─'*W}")
        print(f"  [CELEX 근거 + 관련 조항]  ({b['celex_evidence']['count']}건, 상위 5건)")
        print(f"{'─'*W}")
        for i, c in enumerate(celex_full[:5], 1):
            review_flag = " ⚠ needs_review" if c.get("needs_review") else ""
            dq_flag     = f" [{c.get('document_quality','?')}]"
            passages    = c.get('passages', [])
            print(f"\n  [{i}] {c.get('celex_id','—')}{dq_flag}{review_flag}")
            print(f"    evidence_type  : {c.get('evidence_type','—')}"
                  f"  (weight={c.get('evidence_weight','—')})")
            print(f"    법령 제목      : {(c.get('title_en') or '—')[:100]}")
            print(f"    근거 레이블    : {c.get('evidence_label') or '—'}")
            if passages:
                print(f"    ── 관련 조항 ({len(passages)}건) ──────────────────────")
                for j, p in enumerate(passages, 1):
                    print(f"    [{j}] [{p['match_label']}] '{p['match_term']}'  (pos={p['char_pos']})")
                    print(f"         {p['passage'][:300]}")
            else:
                print(f"    ── 관련 조항: 본문에서 HS 코드 직접 언급 없음")

    # ── CN Notes ─────────────────────────────────────────────
    cn_rows = r["raw"]["cn_note_rows"]
    if cn_rows:
        print(f"\n{'─'*W}")
        print(f"  [CN Explanatory Notes]  (match={b['cn_note']['match']})")
        print(f"{'─'*W}")
        for row in cn_rows[:2]:
            print(f"\n  heading: {row.get('hs_heading','—')}"
                  f"  chapter: {row.get('hs_chapter','—')}")
            print(f"  section: {(row.get('section_title') or '')[:80]}")
            ht = (row.get("heading_text") or "").strip()
            print(f"  heading_text: {ht[:200]}")

    print(f"\n{'='*W}")


# ── CLI ──────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "190230"
    score_hs6(code)

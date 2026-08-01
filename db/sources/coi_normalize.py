"""비정규 제조사 COI(xlsx) → ASAP 정규화 COI 폼(JSON) 변환기.

폼 설계 원칙 (organic_coi_asap_example.pdf 스타일: 헤더 메타 + 구획 테이블):
  - 원문 보존: 성분명은 원문 그대로(name_raw), 창작 없음
  - 구획(section) → role: 제조사 COI의 구성품 컬럼('만두피','양념장','면'…)
    이 법조문 분기(stuffed/sauce/broth)가 묻는 바로 그 구획이다
  - 주성분/부성분: COI에 명시 표기가 없으므로 "표기 1위(용매 제외)" 규칙
    으로 후보만 제안 — solvent(정제수 류)는 주성분 자격에서 구조적으로 제외
  - 범주어 괄호 해체: '곡류가공품(밀가루,전분)' → sub_ingredients 병기
    (군만두 주성분 빈칸 사인의 처방)

출력: data/coi_normalized/coi_<slug>.json + index.json + product_map.csv

실행:
  PYTHONPATH=src python db/sources/coi_normalize.py \
      --src "/Users/snu/Downloads/COI(식품원재료풀이)" \
      --answer-csv data/EU_HS_test2.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = PROJECT_ROOT / "data" / "coi_normalized"

# role 판정 — 구획명(component)의 법조문 대응. 폼 정규화 규칙이며
# 코드/상품 지목이 아니라 서류 양식 어휘의 표준화다.
_ROLE_RULES: tuple[tuple[str, str], ...] = (
    (r"(만두피|피$|도우|반죽|크러스트|wrapper|dough|crust)", "wrapper"),
    (r"(만두소|소$|속$|필링|filling)", "filling"),
    (r"(소스|양념|시즈닝|드레싱|장$|비빔장|sauce|seasoning|dressing)", "sauce"),
    (r"(육수|국물|스프|다시|broth|stock|soup\s*base)", "broth"),
    (r"(면$|면류|생면|숙면|건면|noodle)", "noodle"),
    (r"(고명|토핑|후레이크|건더기|topping|flake)", "topping"),
)
_SOLVENT_RE = re.compile(r"^(정제수|물|정수|음용수|water)$", re.IGNORECASE)
_PAREN_RE = re.compile(r"^(.{2,40}?)\s*[\(（]([^)）]{2,120})[\)）]\s*$")


def _role_for(component: str, ingredient: str) -> str:
    ingredient = unicodedata.normalize("NFC", ingredient)
    component = unicodedata.normalize("NFC", component)
    if _SOLVENT_RE.match(ingredient.strip()):
        return "solvent"
    # 구조 role(wrapper/filling/noodle/broth)은 구획명에서만 — 성분명까지
    # 검사하면 '찰당면'(만두소 재료)이 noodle 구획으로 오인돼 주성분·라우팅
    # 을 오염시킨다(실측: 군만두→1904). 성분명 검사는 sauce 계열만.
    for pattern, role in _ROLE_RULES:
        if re.search(pattern, component.strip(), re.I):
            return role
    liquid = re.search(
        r"(소스|양념|드레싱|시즈닝|육수|국물|sauce|dressing|seasoning|broth|stock)",
        ingredient.strip(),
        re.I,
    )
    if liquid:
        return (
            "broth"
            if liquid.group(0).lower() in {"육수", "국물", "broth", "stock"}
            else "sauce"
        )
    return "other"


def _split_sub(name: str) -> tuple[str, list[str]]:
    """범주어 괄호 해체: '곡류가공품(밀가루,전분)' → ('곡류가공품', [밀가루,전분])."""
    m = _PAREN_RE.match(name.strip())
    if not m:
        return name.strip(), []
    subs = [s.strip() for s in re.split(r"[,、;:/·]", m.group(2)) if len(s.strip()) >= 2]
    # 괄호 안이 원산지·비율 표기('국산','50%')뿐이면 해체하지 않는다
    subs = [s for s in subs if not re.fullmatch(r"[\d.%\s]+|국산|국내산|수입산|외국산", s)]
    return m.group(1).strip(), subs[:8]


def _slug(text: str) -> str:
    # NFD 자모 한글이 [가-힣]에 안 걸려 전부 소거 → 낙지/오징어/주꾸미
    # 세 파일이 같은 슬러그로 덮어써졌다(실측). NFC 재조합 필수.
    text = unicodedata.normalize("NFC", str(text))
    return re.sub(r"[^0-9A-Za-z가-힣]+", "_", text)[:48].strip("_")


def NormalizeCoiFile(path: Path, sheet_name: str | None = None) -> dict:
    from bussiness_logic.product.services.coi_loader import ParseCoiComposition

    raw = ParseCoiComposition(path, sheetName=sheet_name)
    entries = []
    sections: list[str] = []
    for e in raw:
        comp = str(e.get("component") or "")
        if comp and comp not in sections:
            sections.append(comp)
        name, subs = _split_sub(str(e.get("ingredient_name") or ""))
        entries.append({
            "order_index": e.get("order_index"),
            "section": comp,
            "role": _role_for(comp, name),
            "name_raw": str(e.get("ingredient_name") or ""),
            "name_ko": name,
            "sub_ingredients": subs,
            "percent": e.get("percent"),
            "origin": e.get("origin") or "",
        })
    # 주성분은 단수 확정이 아니라 '구획별 대표 후보 1~3' — 김치우동전골의
    # {김치, 우동면, 육수}처럼 구획 대표가 전부 주성분급인 실물을 그대로
    # 담고, 결정(우동을 뽑기)은 주입점의 identity 교차·% 비교가 한다.
    substantive = [e for e in entries if e["role"] not in ("solvent",)]

    def _rep_name(e: dict) -> str:
        return e["sub_ingredients"][0] if e["sub_ingredients"] else e["name_ko"]

    principal_candidates: list[str] = []
    seen_sections: set[str] = set()
    for e in substantive:
        if e["role"] == "sauce":
            continue
        sec = e.get("section") or "(무구획)"
        if sec in seen_sections:
            continue
        seen_sections.add(sec)
        nm = _rep_name(e)
        if nm and nm not in principal_candidates:
            principal_candidates.append(nm)
        if len(principal_candidates) >= 3:
            break
    # 폼은 확정하지 않는다 — 단일 후보(군만두=당면)의 자동 확정이 표기
    # 1위 오염을 부활시킨다. 확정 권한은 주입점의 identity 교차·% 비교만.
    principal = ""
    accessory = []
    for e in substantive:
        nm = _rep_name(e)
        if nm and nm not in principal_candidates and nm not in accessory:
            accessory.append(nm)
        if len(accessory) >= 8:
            break
    # coverage 판정: 원물이 없는 '소스 풀이 전용' 문서 감지 — percent 합이
    # 미미하거나(<30) 실구획이 소스/양념뿐이면 sauce_only. 이런 문서의
    # 표기 1위는 제품의 주성분이 아니라 소스의 1위라 주성분 주입 금지
    # (실측: 주꾸미볶음 COI 162행에 '주꾸미' 0회 — 소스 성분만 존재).
    pct_sum = sum(float(e["percent"]) for e in entries
                  if isinstance(e.get("percent"), (int, float)))
    roles_present = {e["role"] for e in entries}
    substantive_roles = roles_present - {"solvent", "sauce", "other"}
    # 구획명이 제품명과 겹치면 본품 풀이 문서다 — 군만두 구획='군만두'는
    # full, 주꾸미볶음 구획='카레'는 본품 무관(sauce_only 후보).
    hint_nfc = unicodedata.normalize("NFC", path.stem + " " + path.parent.name)
    section_is_product = any(
        s and unicodedata.normalize("NFC", s)[:6] and
        unicodedata.normalize("NFC", s)[:6] in hint_nfc
        for s in sections
    )
    coverage = "full"
    if (entries and pct_sum and pct_sum < 30
            and not substantive_roles and not section_is_product):
        coverage = "sauce_only"

    hint = re.sub(r"^\d*[.\s]*COI[)_\s]*", "", unicodedata.normalize("NFC", path.stem))
    hint = re.sub(r"[_(]?\d{4}[.\-]\d{1,2}[.\-]\d{1,2}[)]?", "", hint)
    return {
        "coi_form_version": "asap-coi-v1",
        "source_file": str(path),
        "product_name_hint": hint.strip(" _-"),
        "folder_hint": path.parent.name,
        "sections": sections,
        "entries": entries,
        "coverage": coverage,
        "principal_candidates": principal_candidates if coverage == "full" else [],
        "principal_ingredient": principal if coverage == "full" else "",
        "accessory_ingredients": accessory if coverage == "full" else [],
        "parse_status": "ok" if entries else "empty",
    }


def _name_tokens(text: str) -> set[str]:
    # macOS 파일명은 NFD(자모 분해)라 [가-힣]이 못 문다 — NFC로 재조합
    text = unicodedata.normalize("NFC", str(text))
    return {t for t in re.findall(r"[가-힣A-Za-z]{2,}", text) if t not in
            {"냉동", "국산", "프리미엄", "오리지널", "정통", "수제", "간편"}}


def _translate_names(names: list[str]) -> dict[str, str]:
    """한글 성분명 → 영문 (LLM 1콜 배치, 전사 검증 — 실패는 빈 매핑 no-op).

    오프라인 정규화 단계라 런타임 비용 0. 원문은 name_ko/raw로 불변 보존,
    영문은 name_en에 병기 — 술어(영문 법조문)와의 lexical 접면용.
    """
    # 사전 캐시: 번역 결과를 ingredient_dictionary.json에 축적 — 같은
    # 성분명은 다시 LLM을 타지 않는다(성분 어휘는 유한, 곧 수렴).
    dict_path = OUT_DIR / "ingredient_dictionary.json"
    cache: dict[str, str] = {}
    try:
        cache = json.loads(dict_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        cache = {}
    todo_all = [n for n in dict.fromkeys(names)
                if n and not all(ord(ch) < 128 for ch in n) and n not in cache]
    out_all: dict[str, str] = dict(cache)
    for start in range(0, len(todo_all), 120):
        out_all.update(_translate_chunk(todo_all[start:start + 120]))
    if len(out_all) > len(cache):
        dict_path.write_text(
            json.dumps(dict(sorted(out_all.items())), ensure_ascii=False, indent=1),
            encoding="utf-8")
        # 오역 탐지의 1선: 신규 LLM 번역분만 검수 큐(CSV)로 적재 — 사람이
        # 훑고 사전 JSON을 직접 고치면 그 값이 영구 우선(캐시 우선 설계라
        # LLM이 기존 키를 덮지 않는다). status는 사람이 지우는 식으로 소비.
        review_path = OUT_DIR / "ingredient_dictionary_review.csv"
        import csv as _csv
        new_keys = sorted(set(out_all) - set(cache))
        exists = review_path.exists()
        with open(review_path, "a", encoding="utf-8-sig", newline="") as fp:
            w = _csv.writer(fp)
            if not exists:
                w.writerow(["name_ko", "name_en(llm)", "교정(비우면 승인)"])
            for k in new_keys:
                w.writerow([k, out_all[k], ""])
        print(f"  성분 사전 갱신: {len(cache)} → {len(out_all)}개"
              f" (신규 {len(new_keys)}건 → 검수 큐 {review_path.name})")
    return out_all


def _translate_chunk(todo: list[str]) -> dict[str, str]:
    try:
        from bussiness_logic.bridge.runtime_adapter import BuildPipelineRuntimeAdapter
        from bussiness_logic.bridge.schema import LlmRequest, LlmGenerationOptions, LlmResponseFormat
        adapter = BuildPipelineRuntimeAdapter()
        prompt = (
            "Translate each Korean food ingredient name to a short English "
            "ingredient term (customs/food-label vocabulary). Return ONLY a "
            "JSON object mapping each input string to its translation.\n"
            + json.dumps(todo, ensure_ascii=False)
        )
        response = adapter.Generate(LlmRequest(
            user_prompt=prompt,
            system_prompt="You translate food ingredient names. Output JSON only.",
            response_format=LlmResponseFormat.JSON_OBJECT,
            generation_options=LlmGenerationOptions(temperature=0.0, max_tokens=2048),
        ))
        raw = json.loads(str(response.generatedText or "{}"))
        out = {}
        for k, v in raw.items():
            if k in todo and isinstance(v, str) and v.strip() and len(v) < 60:
                out[k] = v.strip()
        return out
    except Exception as error:  # noqa: BLE001
        print(f"  (번역 생략: {type(error).__name__}: {str(error)[:60]})")
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="/Users/snu/Downloads/COI(식품원재료풀이)")
    ap.add_argument("--answer-csv", default=str(PROJECT_ROOT / "data" / "EU_HS_test2.csv"))
    ap.add_argument("--translate", action="store_true",
                    help="한글 성분명 name_en 배치 번역 (LLM 1~2콜)")
    args = ap.parse_args()
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    forms: list[dict] = []
    for path in sorted(Path(args.src).rglob("*.xlsx")):
        if path.name.startswith("~$"):
            continue
        form = NormalizeCoiFile(path)
        out = OUT_DIR / f"coi_{_slug(path.parent.name)}__{_slug(path.stem)}.json"
        out.write_text(json.dumps(form, ensure_ascii=False, indent=1), encoding="utf-8")
        form["_out"] = out.name
        forms.append(form)
        mark = "✓" if form["parse_status"] == "ok" else "✗빈결과"
        print(f"  {mark} {path.parent.name[:22]:<24} {path.name[:36]:<38} "
              f"entries={len(form['entries']):>2} 주성분={form['principal_ingredient'][:14]}")

    ok = [f for f in forms if f["parse_status"] == "ok"]
    print(f"\n정규화 완료: {len(ok)}/{len(forms)} 파일 (빈 결과 {len(forms) - len(ok)})")

    if args.translate:
        names = []
        for f in ok:
            for e in f["entries"]:
                names.append(e["name_ko"])
                names.extend(e.get("sub_ingredients") or [])
            names.extend(f.get("principal_candidates") or [])
        mapping = _translate_names(names)
        print(f"번역 매핑: {len(mapping)}개 성분명")
        if mapping:
            for f in ok:
                for e in f["entries"]:
                    if e["name_ko"] in mapping:
                        e["name_en"] = mapping[e["name_ko"]]
                f["principal_candidates_en"] = [
                    mapping.get(c, "") for c in f.get("principal_candidates") or []]
                (OUT_DIR / f["_out"]).write_text(
                    json.dumps({k: v for k, v in f.items() if k != "_out"},
                               ensure_ascii=False, indent=1), encoding="utf-8")

    # 답지 제품명 → 정규화 COI 매핑 (토큰 겹침 최다 — 사용자 교정용 CSV)
    rows = []
    try:
        for r in csv.DictReader(open(args.answer_csv, encoding="utf-8-sig")):
            name = str(r.get("제품명") or "").strip()
            if not name:
                continue
            ntoks = _name_tokens(name)
            best, best_score = "", 0
            for f in ok:
                # 부분문자열 스코어: '들깨미역국'(붙은 표기) 안에서 '들깨'·
                # '미역국' 토큰을 각각 인정 — 토큰 교집합은 붙은 한글 표기에
                # 전멸한다(실측 오매핑 14건). 힌트 쪽 긴 토큰의 역방향도 인정.
                hint_text = unicodedata.normalize(
                    "NFC", f["product_name_hint"] + " " + f["folder_hint"])
                score = sum(1 for tk in ntoks if tk in hint_text)
                score += sum(1 for tk in _name_tokens(hint_text)
                             if len(tk) >= 3 and tk in unicodedata.normalize("NFC", name))
                if score > best_score:
                    best, best_score = f["_out"], score
            rows.append({"제품명": name, "coi_file": best, "match_tokens": best_score})
        # 다중 COI 결합: 매칭된 파일과 같은 폴더의 나머지 파일들(다른 상품이
        # 최우수 매칭으로 가져가지 않은 것)을 같은 상품에 병합 귀속 —
        # 밀키트(김치우동전골)는 구성품(어묵·김치·면·소스)마다 COI가 따로다.
        claimed = {r["coi_file"] for r in rows if r["coi_file"]}
        by_folder: dict[str, list] = {}
        for f in ok:
            by_folder.setdefault(f["folder_hint"], []).append(f)
        for r in rows:
            if not r["coi_file"]:
                continue
            primary = next((f for f in ok if f["_out"] == r["coi_file"]), None)
            if not primary:
                continue
            # 과잉 귀속 가드: 같은 폴더라도 형제 '상품'(낙지 폴더의 오징어
            # 볶음)은 붙이면 오염이다. 붙일 자격 = ①상품명과 비제네릭 토큰
            # 겹침(구성품/동일상품) 또는 ②주 파일과 힌트 2토큰 이상 겹침
            # (수출판 같은 판본 중복 — 병합해도 합집합이라 무해).
            generic = {"볶음", "볶음밥", "냉동", "국산", "간편", "수출", "가능",
                       "원재료", "풀이", "정보", "양식"}
            ptoks = _name_tokens(r["제품명"]) - generic
            prim_toks = _name_tokens(primary["product_name_hint"]) - generic
            extras = []
            for f in by_folder.get(primary["folder_hint"], []):
                if f["_out"] == r["coi_file"] or f["_out"] in claimed:
                    continue
                etoks = _name_tokens(f["product_name_hint"]) - generic
                hit_product = any(tk in " ".join(etoks) or any(tk in e for e in etoks)
                                  for tk in ptoks)
                hit_version = len(etoks & prim_toks) >= 2
                if hit_product or hit_version:
                    extras.append(f["_out"])
            if extras:
                r["coi_file"] = ";".join([r["coi_file"], *extras])
    except FileNotFoundError:
        print(f"답지 없음: {args.answer_csv} — product_map 생략")
    if rows:
        map_path = OUT_DIR / "product_map.csv"
        with open(map_path, "w", encoding="utf-8-sig", newline="") as fp:
            w = csv.DictWriter(fp, fieldnames=["제품명", "coi_file", "match_tokens"])
            w.writeheader()
            w.writerows(rows)
        unmatched = sum(1 for r in rows if not r["coi_file"] or r["match_tokens"] == 0)
        print(f"product_map.csv: {len(rows)}행 (미매칭 {unmatched} — 파일 열어 coi_file 수기 교정 가능)")

    index = [{k: f[k] for k in ("_out", "product_name_hint", "folder_hint",
                                "parse_status", "principal_ingredient")} for f in forms]
    (OUT_DIR / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""HS4/HS6/CN8 axis verdict runtime.

DB/redesign_hs4.py(오프라인 실험)의 판정핵을 런타임으로 이식한 것. 병립
선택기를 새로 만들지 않는다 — 이 모듈은 ranked 엔트리에 **verdict를 찍을
뿐**이고, 선택은 기존 `_decide_ifelse`(이산 캐스케이드) + `_ApplyGri3Law`
(법정 GRI3)가 소비한다 = 런타임 권위 단일화.

HS4·HS6·CN8은 DTO projection primitives만 공유한다. 공개 진입점과 축맵은
각각 `StampHs4AxisVerdicts`/`heading_axis_map`,
`StampHs6AxisVerdicts`/`subheading_axis_map`,
`StampCn8AxisVerdicts`/`cn8_axis_map`으로 분리한다.

판정(스코어 0·이산):
  - 명시적 signed predicate는 broad axis보다 우선한다. FALSE는 배제,
    UNKNOWN은 SILENCE, TRUE는 확정이다. 따라서 ``stuffed`` 질문을
    ``cooked`` 상태만으로 확정할 수 없다.
  - heading의 축(DB heading_axis_map, 1227 heading 검수본) → 그 축의
    canonical DTO binding만 조회(axis_field_binding 단일 registry —
    product_identity→commodity_identity 등. 성분 cereal 오염 차단).
  - canonical 필드가 비면 SILENCE, 채워졌으면 질문 충족 O / 불충족 X.
  - 상태 나열은 OR로 평가한다(desc 'fresh, chilled or frozen'에서 frozen=O).
  - NESOI/Other 잔반 heading은 O여도 confirmed가 아니라 residual 마킹
    (2-pass: 명시 O 우선, 잔반은 명시 없을 때 — _decide_ifelse 잔반 가지).
  - 기존 법정 라벨 보존: 이 스탬프는 undecided만 채운다. 결정테이블이
    이미 violated/confirmed를 세운 엔트리는 건드리지 않는다(법정 조건이
    토큰 겹침보다 상위 법원).
  - 상태 나열은 OR로 해석하며, 상태 모순은 상태 축에만 적용한다.
  - 제품 정체성 축은 상품 형태의 원자적 head만 읽는다. NTD의 부속 성분
    (``fish cake``)이나 광역 성분 토큰은 정체 확정에 사용하지 않는다.

의존: PostgreSQL heading_axis_map/subheading_axis_map, co_loader
ExpandTaxonomy(결정론 문자열 매칭·점수 없음).
HS4/HS6 staged runtime에서 항상 호출되며 실험용 킬스위치는 없다.
"""
from __future__ import annotations

import re
from typing import Any

from bussiness_logic.classification.rules.axis_field_binding import (
    ResolvedAxisBinding,
    ResolveAxisFieldBinding,
)
from bussiness_logic.core.runtime_asset_repository import LoadSingletonAsset

_STOP = frozenset({
    "and", "or", "of", "the", "other", "with", "for", "not", "than",
    "containing", "prepared", "preserved", "whether", "kind", "food",
    "product", "products", "n.e.s", "nes", "from", "made", "obtained",
})

_STATES = ("cooked", "uncooked", "dried", "frozen", "fresh", "chilled",
           "smoked", "salted", "boiled", "live", "brine")
_IDENTITY_AXIS_FAMILY = frozenset({
    "product_identity",
    "species_source",
    "material_composition",
})
_NESOI = ("not elsewhere specified", "not elsewhere included", "n.e.s")

_FORM_VOCAB = frozenset({
    "extract", "juice", "whole", "piece", "fillet", "flake", "powder",
    "paste", "granule", "block", "minced", "strip", "meal", "flour",
    "pellet", "ground", "sliced", "slice", "pearl", "grain", "sifting",
    "stuffed",
})

_TOKEN_RE = re.compile(r"[a-z]{3,}")
_axmap_cache: list[dict] = []


def _stem(t: str) -> str:
    """어미 's'만 제거(noodles→noodle). 'es' 일괄제거는 noodl 오류라 금지."""
    if len(t) > 3 and t.endswith("s"):
        return t[:-1]
    return t


def _toks(s: Any) -> set:
    return {t for t in _TOKEN_RE.findall(str(s or "").lower())
            if t not in _STOP}


def _stemset(ts) -> set:
    return {_stem(t) for t in ts}


def _axmap() -> dict:
    if _axmap_cache:
        return _axmap_cache[0]
    m = LoadSingletonAsset("heading_axis_map").get("headings") or {}
    _axmap_cache.append(m)
    return m


_sub_axmap_cache: list[dict] = []


def _sub_axmap() -> dict:
    if _sub_axmap_cache:
        return _sub_axmap_cache[0]
    m = LoadSingletonAsset("subheading_axis_map").get("subheadings") or {}
    _sub_axmap_cache.append(m)
    return m


_cn8_axmap_cache: list[dict] = []


def _cn8_axmap() -> dict:
    if _cn8_axmap_cache:
        return _cn8_axmap_cache[0]
    m = LoadSingletonAsset("cn8_axis_map").get("cn8") or {}
    _cn8_axmap_cache.append(m)
    return m


def _hs4_axis_for(code: str) -> str:
    """Return the heading axis for one HS4 code."""
    digits = re.sub(r"\D", "", str(code or ""))
    rec = _axmap().get(digits[:4]) or {}
    return str(rec.get("axis") or "product_identity")


def _hs6_axis_for(code: str) -> str:
    """Return the subheading axis for one HS6 code."""
    digits = re.sub(r"\D", "", str(code or ""))
    rec = _sub_axmap().get(digits[:6]) or {}
    return str(rec.get("axis") or "product_identity")


def _cn8_axis_for(code: str) -> str:
    """Return the CN8 axis for one eight-digit code."""
    digits = re.sub(r"\D", "", str(code or ""))
    rec = _cn8_axmap().get(digits[:8]) or {}
    return str(rec.get("axis") or "product_identity")


def GetCn8AxisRecord(code: str) -> dict:
    """Return immutable-by-convention CN8 map metadata for runtime scoping."""
    digits = re.sub(r"\D", "", str(code or ""))
    return dict(_cn8_axmap().get(digits[:8]) or {})


def ProjectDecisionRowsForAxis(
    level: str,
    code: str,
    conditions: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Project fragmented compiler rows onto one canonical branch question.

    HS4/HS6/CN8 each own exactly one primary axis per code. Legacy compiler
    output can contain several condition families for the same code; reducing them as
    an implicit AND makes an affirmative species answer lose to an unrelated
    product-identity mismatch. Identity/species/material rows describe
    alternative views of the same commodity identity, so they form one OR
    group. Other axes keep their compiler-authored AND/OR structure.

    Qualifiers are scope metadata, not the code's O/X question. They remain in
    the sidecar for audit but are not promoted to answer authority here.
    """
    axis_for = {
        "hs4": _hs4_axis_for,
        "hs6": _hs6_axis_for,
        "cn8": _cn8_axis_for,
    }.get(level)
    if axis_for is None:
        return "", []
    primary_axis = axis_for(code)
    # Identity questions often need several equivalent views of the same
    # commodity. Only an exact-species branch is narrower than that family:
    # a generic product_identity row must not prove a species_source leaf such
    # as "Pandalus/Crangon". Other axes retain the reviewed pre-existing OR
    # projection until their signed-polarity conditions have equivalent
    # canonical authority.
    allowed = (
        frozenset({primary_axis})
        if level in ("hs6", "cn8") and primary_axis == "species_source"
        else (
            _IDENTITY_AXIS_FAMILY
            if primary_axis in _IDENTITY_AXIS_FAMILY
            else frozenset({primary_axis})
        )
    )
    projected: list[dict[str, Any]] = []
    for condition in conditions or []:
        if str(condition.get("role") or "").strip() == "qualifier":
            continue
        cond_type = str(condition.get("cond_type") or "").strip()
        if cond_type not in allowed:
            continue
        row = dict(condition)
        if primary_axis in _IDENTITY_AXIS_FAMILY:
            row["alt_group"] = f"{code}:primary_axis"
        projected.append(row)
    return primary_axis, projected


def _expand(ts: set) -> set:
    """taxonomy 확장(octopus→mollusc) — 결정론 문자열 매칭, 실패=무확장."""
    out = set(ts)
    try:
        from bussiness_logic.product.services.co_loader import ExpandTaxonomy
        out |= {t for x in ExpandTaxonomy(sorted(ts)) for t in _toks(x)}
    except Exception:  # noqa: BLE001
        pass
    return out


def _head_tokens(values: list[object]) -> set[str]:
    """Return product-form heads without decomposing compound identities.

    ``rice cake`` and ``fish cake`` therefore remain cake forms, while
    ``prepared rice meal`` remains a meal rather than becoming generic rice.
    """
    out: set[str] = set()
    for value in values:
        tokens = [
            _stem(token)
            for token in _TOKEN_RE.findall(str(value or "").lower())
            if token not in _STOP
        ]
        if tokens:
            out.add(tokens[-1])
    return out


def BuildDtoFields(product_facts: dict) -> dict:
    """DTO를 축 라우팅용 필드별 토큰군으로 (redesign_hs4._dto_fields 이식)."""
    ih = product_facts.get("identity_hints") or {}
    cf = product_facts.get("composition_facts") or {}
    identity_heads = _head_tokens([
        ih.get("commercial_identity"),
        ih.get("food_form"),
    ])
    ff = _expand(_toks(ih.get("food_form")))
    prin = _expand(_toks(cf.get("principal_ingredient"))
                   | _toks(ih.get("principal_ingredient_guess")))
    ntd = _expand(_toks(ih.get("normalized_tariff_description"))
                  | {t for x in (ih.get("identity_terms") or [])
                     for t in _toks(x)})
    comp = _expand({t for c in (cf.get("ingredient_classes") or [])
                    for t in _toks(c)}
                   | _toks(cf.get("principal_ingredient")))
    pform = _expand({t for x in (ih.get("product_form_terms") or [])
                     for t in _toks(x)})
    physical = _expand(
        _toks(ih.get("physical_form"))
        | _toks(cf.get("physical_form"))
        | pform
    )
    preservation = _stemset(
        _toks(ih.get("preservation_state"))
        | _toks(cf.get("preservation_state"))
    )
    processing = _stemset(
        _toks(ih.get("processing_state"))
        | _toks(cf.get("processing_state"))
    )
    # [서열 가드 재료 · 07-23] 성분 서열(1순위 vs 부성분) 토큰 — staged
    # order_guard("성분에 있다≠그 성분의 제품이다")와 동일 계보를 축
    # 스탬프에 이식하기 위한 실물. ingredient_entries의 order_index 소비.
    rank1: set = set()
    acc: set = set()
    for e in (cf.get("ingredient_entries") or []):
        if not isinstance(e, dict):
            continue
        nm = str(e.get("ingredient_name") or "")
        if " / " in nm:  # "KO / EN" 병기 — EN부만
            nm = nm.split(" / ", 1)[1]
        etoks = _toks(nm)
        for a in (e.get("term_aliases") or []):
            etoks |= _toks(a)
        try:
            rank = int(e.get("order_index") or 99)
        except (TypeError, ValueError):
            rank = 99
        if rank == 1:
            rank1 |= etoks
        else:
            acc |= etoks
    rank1 = _stemset(_expand(rank1))
    acc = _stemset(_expand(acc)) - rank1  # 1순위와 겹치면 1순위 자격
    return {"identity_head": identity_heads,
            "food_form": _stemset(ff),
            "principal": _stemset(prin),
            "ntd": _stemset(ntd), "composition": _stemset(comp),
            "product_form": _stemset(pform),
            "physical_form": _stemset(physical),
            "preservation_state": preservation,
            "processing_state": processing,
            "rank1": rank1, "accessory": acc}


def _is_resid_desc(desc: str) -> bool:
    d = desc.strip().lower()
    # [07-24 수리] "Other," "Other:" 등 구두점 접두 포함 — 030619
    # "Other, including flours…"가 명시로 오인되던 실측.
    if d == "other" or re.match(r"^other\b", d):
        return True
    # NESOI at the end of a substantive nomenclature sentence is a legal
    # boundary, not proof that the entire code is a residual leaf. Only a
    # bare/leading NESOI label is itself a residual branch.
    return any(d.startswith(k) for k in _NESOI)


def _is_level_residual(desc: str, axis: str, source_map: str) -> bool:
    """Recognise a residual without leaking HS4 NESOI rules into CN8.

    Heading-level ``exclusion_boundary`` entries such as 2106 are the
    complement of the named headings in their chapter even though the NESOI
    phrase appears at the end. CN8 keeps the stricter leading/bare rule until
    its reviewed map carries an explicit residual role.
    """
    if _is_resid_desc(desc):
        return True
    normalized = str(desc or "").strip().lower()
    return (
        source_map == "hs4_axis_map"
        and axis == "exclusion_boundary"
        and any(marker in normalized for marker in _NESOI)
    )


def _verdict(
    axis: str,
    desc: str,
    binding: ResolvedAxisBinding,
    fld: dict,
) -> str:
    """Return the discrete verdict O/X/resid/none for one bound axis."""
    eff = str(desc or "").strip()
    if not eff:
        return "none"
    if _is_resid_desc(eff):
        return "resid"
    low = eff.lower()
    htoks = _stemset(_toks(eff))
    dto_toks = set(binding.tokens)
    if htoks & set(binding.deniedTokens):
        return "X"
    if binding.status != "answered":
        return "none"
    if axis in ("preservation_state", "processing_method",
                "condition_quality", "processing_state"):
        state_words = {st for st in _STATES if re.search(rf"\b{st}\b", low)}
        if not state_words:
            return "none"
        return "O" if state_words & dto_toks else "X"
    if axis == "physical_form":
        htoks = htoks & _FORM_VOCAB  # 형태어만(1603 molluscs false-O 차단)
        if not htoks:
            return "none"
    matched = htoks & dto_toks
    if not matched:
        return "X"
    # [서열 가드 · 07-23] 種/성분축의 O가 **부성분-only 교차**면 자격 박탈
    # (SILENT). 실측: 짬뽕의 오징어(부성분)가 1605를 confirmed로 만들어
    # 교차챕터 3(c) 동전던지기의 재료가 됨 — staged order_guard("성분에
    # 있다≠그 성분의 제품이다")를 스탬프에 이식. 서열 정보 없으면(entries
    # 부재) 종전 동작. 주성분 교차가 하나라도 있으면 O 유지.
    if axis in ("species_source", "material_composition"):
        acc = fld.get("accessory") or set()
        rank1 = fld.get("rank1") or set()
        if acc and (matched & acc) and not (matched & rank1) \
                and not (matched - acc):
            return "X"
    return "O"


def _silence_reason(
    axis: str,
    desc: str,
    binding: ResolvedAxisBinding,
) -> str:
    if binding.status == "unsupported":
        return "canonical_axis_unsupported"
    if binding.status != "answered":
        return "canonical_field_empty"
    low = str(desc or "").lower()
    if axis in {
        "preservation_state",
        "processing_method",
        "condition_quality",
        "processing_state",
    } and not any(re.search(rf"\b{state}\b", low) for state in _STATES):
        return "question_not_compiled_for_axis"
    if axis == "physical_form" and not (_stemset(_toks(desc)) & _FORM_VOCAB):
        return "question_not_compiled_for_axis"
    return "question_not_answerable"


def _signed_predicate_authority(entry: dict[str, Any]) -> str:
    """Return O/X/SILENCE when a decisive signed question was evaluated.

    Explicit user answers are already folded into ``decision`` before this
    layer. An overridden original detail is ignored here. Multiple signed
    predicates use AND semantics: one X excludes, one unresolved predicate
    keeps the branch silent, and only an all-O set confirms.
    """
    verdicts: list[str] = []
    for detail in entry.get("predicate_results") or []:
        if not isinstance(detail, dict):
            continue
        if detail.get("overridden_by"):
            continue
        if str(detail.get("authority") or "") != "signed_polarity":
            continue
        verdict = str(detail.get("verdict") or "").strip().lower()
        if verdict in {"false", "true"}:
            verdicts.append(verdict)
        elif verdict in {"", "unknown", "undecided", "silent"}:
            verdicts.append("unknown")
    if not verdicts:
        return ""
    if "false" in verdicts:
        return "X"
    if "unknown" in verdicts:
        return "SILENCE"
    return "O"


def _stamp_axis_verdicts(
    ranked: list,
    product_facts: dict,
    *,
    axis_for,
    source_map: str,
    map_available: bool,
) -> int:
    """Stamp one classification level using its own axis resolver.

    undecided만 채운다(법정 라벨 보존). O→confirmed(잔반 heading이면
    residual 마킹만) · X→violated · none→무접촉(SILENT). decision_detail에
    cond=축·op=axis_verdict·why=<level>_axis_map 병기(감사 실물)."""
    if not ranked:
        return 0
    if not map_available:
        return 0
    fld = BuildDtoFields(product_facts or {})
    # [07-24 · 2-pass 원칙의 귀결] 잔반 형제 어휘 합집합 — "명시 O"의
    # 자격은 잔반이 못 덮는 토큰 최소 1개(범용 'shrimp'가 특정종 소호
    # 030616을 confirmed로 만들던 실측 처방 — 명시 O 우선 원칙은 진짜
    # 명시일 때만 성립한다). 잔반 없음 = 강등 없음(종전 동작).
    resid_toks: set = set()
    for e0 in ranked:
        d0 = str(e0.get("descr") or "")
        if _is_resid_desc(d0):
            resid_toks |= _stemset(_toks(d0))
    stamped = 0
    for e in ranked:
        code = str(e.get("code") or "")
        desc = str(e.get("descr") or "")
        axis = axis_for(code)
        if bool(e.get("residual")) or _is_level_residual(
            desc,
            axis,
            source_map,
        ):
            # "Other"/NESOI is the complement of its named siblings.  It can
            # never earn O from its inherited axis or a legacy signed row.
            # Selection is legal only after parent/context-local elimination.
            e["residual"] = True
            if str(e.get("decision") or "") in {"confirmed", "violated"}:
                e["decision"] = "undecided"
            e.setdefault("decision_detail", []).append({
                "cond": axis_for(str(e.get("code") or "")),
                "op": "axis_verdict",
                "verdict": "residual",
                "why": f"{source_map}:complement_only",
                "value": desc[:60],
            })
            stamped += 1
            continue
        if str(e.get("decision") or "") in ("confirmed", "violated"):
            continue  # 법정 결정테이블 라벨이 상위 법원
        signed_verdict = _signed_predicate_authority(e)
        if signed_verdict:
            detail = {
                "cond": axis,
                "op": "axis_verdict",
                "value": desc[:60],
                "binding_axis": axis,
            }
            if signed_verdict == "X":
                e["decision"] = "violated"
                e.setdefault("decision_detail", []).append({
                    **detail,
                    "verdict": "false",
                    "why": f"{source_map}:signed_polarity_false",
                })
            elif signed_verdict == "SILENCE":
                e.setdefault("decision_detail", []).append({
                    **detail,
                    "verdict": "silent",
                    "why": f"{source_map}:signed_polarity_unresolved",
                })
            else:
                e["decision"] = "confirmed"
                e.setdefault("decision_detail", []).append({
                    **detail,
                    "verdict": "true",
                    "why": f"{source_map}:signed_polarity_true",
                })
            stamped += 1
            continue
        question_tokens = frozenset(_stemset(_toks(desc)))
        binding = ResolveAxisFieldBinding(
            axis,
            product_facts,
            questionTokens=question_tokens,
        )
        binding_trace = binding.ToTrace()
        taxonomy_reason = ""
        if axis == "species_source":
            from bussiness_logic.classification.rules.species_taxonomy import (
                EvaluateSpeciesQuestion,
            )

            taxonomy_result = EvaluateSpeciesQuestion(
                [desc],
                product_facts,
                binding.paths,
            )
            binding_trace.update(taxonomy_result.ToTrace())
            taxonomy_reason = taxonomy_result.reason
            v = {
                "O": "O",
                "X": "X",
                "SILENCE": "none",
            }[taxonomy_result.verdict]
        else:
            v = _verdict(axis, desc, binding, fld)
        if v == "none":
            e.setdefault("decision_detail", []).append({
                "cond": axis,
                "op": "axis_verdict",
                "verdict": "silent",
                "why": (
                    f"{source_map}:exact_taxonomy:{taxonomy_reason}"
                    if taxonomy_reason
                    else f"{source_map}:{_silence_reason(axis, desc, binding)}"
                ),
                "value": desc[:60],
                **binding_trace,
            })
            stamped += 1
            continue
        det = e.setdefault("decision_detail", [])
        if v == "O":
            if _is_resid_desc(desc):
                e["residual"] = True  # 잔반 O — 명시 O 없을 때만 (2-pass)
            else:
                if axis in {
                    "preservation_state",
                    "processing_method",
                    "processing_state",
                    "condition_quality",
                } and any(
                    "state_alone_blocked" in str(item.get("why") or "")
                    for item in det
                    if isinstance(item, dict)
                ):
                    det.append({
                        "cond": axis,
                        "op": "axis_verdict",
                        "verdict": "skipped",
                        "why": f"{source_map}:state_alone_insufficient",
                        "value": desc[:60],
                        **binding_trace,
                    })
                    stamped += 1
                    continue
                # 명시 자격 심사: 매칭 토큰이 전부 잔반 어휘에 덮이면
                # "명시"가 아니다 → 강등(SILENT — 잔반 가지가 수용)
                _dto_all = set(binding.tokens)
                _m = _stemset(_toks(desc)) & _dto_all
                if resid_toks and _m and _m <= resid_toks:
                    det.append({"cond": axis, "op": "axis_verdict",
                                "verdict": "skipped",
                                "why": "generic_covered_by_residual",
                                "value": desc[:60],
                                **binding_trace})
                    stamped += 1
                    continue
                e["decision"] = "confirmed"
            det.append({"cond": axis, "op": "axis_verdict", "verdict": "true",
                        "why": (
                            f"{source_map}:exact_taxonomy:{taxonomy_reason}"
                            if taxonomy_reason
                            else source_map
                        ), "value": desc[:60],
                        **binding_trace})
        elif v == "X":
            e["decision"] = "violated"
            det.append({"cond": axis, "op": "axis_verdict", "verdict": "false",
                        "why": (
                            f"{source_map}:exact_taxonomy:{taxonomy_reason}"
                            if taxonomy_reason
                            else f"{source_map}:canonical_field_mismatch"
                        ),
                        "value": desc[:60],
                        **binding_trace})
        elif v == "resid":
            e["residual"] = True
            det.append({"cond": axis, "op": "axis_verdict",
                        "verdict": "residual", "why": source_map,
                        "value": desc[:60],
                        **binding_trace})
        stamped += 1
    return stamped


def StampHs4AxisVerdicts(ranked: list, product_facts: dict) -> int:
    """Stamp HS4 headings from the heading axis map only."""
    return _stamp_axis_verdicts(
        ranked,
        product_facts,
        axis_for=_hs4_axis_for,
        source_map="hs4_axis_map",
        map_available=bool(_axmap()),
    )


def StampHs6AxisVerdicts(ranked: list, product_facts: dict) -> int:
    """Stamp HS6 subheadings from the subheading axis map only."""
    return _stamp_axis_verdicts(
        ranked,
        product_facts,
        axis_for=_hs6_axis_for,
        source_map="hs6_axis_map",
        map_available=bool(_sub_axmap()),
    )


def StampCn8AxisVerdicts(ranked: list, product_facts: dict) -> int:
    """Stamp CN8 leaves from the canonical CN8 axis map only."""
    stamped = _stamp_axis_verdicts(
        ranked,
        product_facts,
        axis_for=_cn8_axis_for,
        source_map="cn8_axis_map",
        map_available=bool(_cn8_axmap()),
    )
    axis_map = _cn8_axmap()
    for row in ranked:
        code = re.sub(r"\D", "", str(row.get("code") or ""))[:8]
        record = axis_map.get(code) or {}
        row["axis_parent_code"] = str(record.get("decision_parent_code") or "")
        row["axis_parent_level"] = str(record.get("decision_parent_level") or "")
        row["axis_runtime_parent"] = str(record.get("runtime_parent_code") or "")
    return stamped

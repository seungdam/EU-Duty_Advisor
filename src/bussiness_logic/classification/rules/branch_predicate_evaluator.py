"""Three-valued branch predicate evaluator (runtime, no LLM).

Reads the ``branch_predicate_index`` sidecar (built offline by
branch_predicate_compiler) and evaluates each branch's predicates against the
product's fact tokens:

  true     condition satisfied            -> boost
  false    condition violated             -> hard penalty (effectively out)
  unknown  DTO cannot answer              -> neutral (0) — NOT a mismatch

The score contribution is ADDITIVE over the lexical score so siblings with
and without compiled predicates stay on a symmetric footing (a mode switch
was measured to create unfair sibling fights when only some rows carried
required_dto_fields). ``pct`` predicates are delegated to the existing quant
gate and ``residual`` markers to the elimination policy — both skipped here.

Degrades to no-op ({} / 0.0) on any DB failure, per repository convention.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

_FIELD_BOOST = 3.0   # the bound DTO field answers the question directly
_POOL_BOOST = 1.0    # only the broad token pool answers — weak support
_PREDICATE_BLOCK = 100.0  # mirrors QUANT_PENALTY: a violated condition is out

# pct/quantitative_threshold는 quant 게이트가 원문 기준으로 별도 처리,
# residual은 소거 정책 소관 — 술어 평가에서 제외 (v1/v2 세대 공통)
_SKIP_AXES = frozenset({"pct", "residual", "quantitative_threshold", "residual_other"})
# IS-A alias (cn_alias_index, compiled offline from the CN tree itself):
# product token "octopus" answers a class question "molluscs". Applied to
# species/contains only — physical-state axes have no IS-A structure.
_ALIAS_AXES = frozenset({
    "species", "contains",  # llm-v1 명칭
    "species_source", "material_composition", "product_identity",  # llm-v2(taxonomy)
})
_alias_cache: list[dict[str, list[str]]] = []


def _aliases() -> dict[str, list[str]]:
    if not _alias_cache:
        loaded: dict[str, list[str]] = {}
        try:
            from sqlalchemy import text

            from db.db_session_manager import DbSessionManager

            version = (os.environ.get("ASAP_ALIAS_VERSION", "tree-v1") or "").strip()
            for row in DbSessionManager.GetInstance().FetchRows(
                text('SELECT token, class_tokens FROM "cn_alias_index"'
                     " WHERE alias_version = :version"),
                {"version": version},
            ):
                data = dict(row)
                try:
                    loaded[str(data.get("token"))] = list(json.loads(str(data.get("class_tokens") or "[]")))
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001 — sidecar absent = no expansion
            loaded = {}
        _alias_cache.append(loaded)
    return _alias_cache[0]


def _expand(tokens: set[str]) -> set[str]:
    alias = _aliases()
    if not alias:
        return tokens
    out = set(tokens)
    for token in tokens:
        out.update(alias.get(token, ()))
    return out
# form/processing answer PHYSICAL-STATE questions: only the dedicated fields
# may answer them — a stray OCR word in the broad pool must not satisfy a
# form predicate (designer decision). species/contains keep the graded pool
# fallback because ingredient words legitimately live in many fields.
_STRICT_FIELD_AXES = frozenset({
    "form", "processing",  # llm-v1 명칭
    "physical_form", "preservation_state", "processing_method",  # llm-v2(taxonomy)
    "condition_quality",
})
_PATH_STRUCTURE_WORDS = frozenset({"contains", "is", "has", "or", "and"})
_TOKEN = re.compile(r"[a-z]+")


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("es") and not token.endswith(("ses", "oes")):
        return token[:-2] if token.endswith(("ches", "shes", "xes")) else token[:-1]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _dig(obj: Any, path: str) -> Any:
    cur = obj
    for key in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
    return cur


# 센티널은 '채워짐'이 아니라 '모름'이다 — 토큰으로 흘리면 field_empty가
# field_no_match로 위장된다 (comp.processing_state='unknown' 22/22 실측).
_FIELD_SENTINELS = frozenset({"unknown", "other", "none", "n/a", "na"})
_NEGATION_WORDS = frozenset({"no", "not", "without", "non", "un"})
_NEGATION_PREFIXES = ("non", "un")
_MILLING_MEAL_CONTEXT = frozenset({
    "bone", "cereal", "feed", "fish", "flour", "grain", "groat",
    "ground", "milled", "milling", "oilcake", "powder", "seed",
})
_PREPARED_MEAL_CONTEXT = frozenset({
    "cooked", "dish", "kit", "mixed", "noodle", "prepared", "ready",
    "serving",
})
_FORM_POLARITY_EQUIVALENTS = {
    "stuffed": frozenset({"stuffed", "filled"}),
    "filled": frozenset({"stuffed", "filled"}),
}
_PROCESSING_POLARITY = {
    "uncooked": (
        frozenset({"uncooked", "requires_cooking"}),
        frozenset({"cooked"}),
    ),
    "cooked": (
        frozenset({"cooked"}),
        frozenset({"uncooked", "requires_cooking"}),
    ),
}
_WRAPPER_FIELD = "composition_facts.contains_wrapper_or_dough"


def _field_tokens(product_facts: Mapping[str, Any] | None, dto_field: str) -> set[str]:
    """Token set of the bound DTO fields (';'-separated paths)."""
    if not product_facts or not dto_field or dto_field == "*tokens*":
        return set()
    out: set[str] = set()
    for path in dto_field.split(";"):
        path = path.strip()
        value = _dig(product_facts, path)
        parts: list[str] = []
        if isinstance(value, bool):
            # booleans answer the question their field NAME asks
            # (contains_sauce_or_broth=True -> sauce/broth)
            if value:
                leaf = path.rsplit(".", 1)[-1]
                parts = [w for w in leaf.split("_") if w not in _PATH_STRUCTURE_WORDS]
        elif isinstance(value, str):
            parts = [] if value.strip().lower() in _FIELD_SENTINELS else [value]
        elif isinstance(value, list):
            parts = []
            # [8회차-4] 성분 물량 내성 — 대량 성분 목록(COI 30항 등)이
            # 정체·종 술어를 ANY-match로 납치하던 지대(꼬막장 0307행·추어탕)
            # 의 처방: 표기 서열(order_index) 상위 N만 술어 답안에 관여.
            # 어휘 제거가 아니라 '관여 제한' — BTI 쿼리·조성 질문 경로는
            # 전체 entries를 그대로 본다. ASAP_ENTRY_RANK_CAP(기본 8), 0=무제한.
            try:
                _cap = int((os.environ.get(
                    "ASAP_ENTRY_RANK_CAP", "8") or "8").strip())
            except ValueError:
                _cap = 8
            for v in value:
                if isinstance(v, dict):
                    # 구조화 엔트리(ingredient_entries 등): 성분명만 읽는다
                    # — 라벨이든 COI든 존재하는 쪽이 그대로 답안이 된다.
                    if _cap > 0:
                        try:
                            _oi = int(v.get("order_index") or 0)
                        except (TypeError, ValueError):
                            _oi = 0
                        if _oi > _cap:
                            continue
                    name = str(v.get("ingredient_name") or v.get("name") or "")
                    if name.strip():
                        parts.append(name)
                elif str(v).strip().lower() not in _FIELD_SENTINELS:
                    parts.append(str(v))
        for part in parts:
            out |= {_stem(t) for t in _TOKEN.findall(part.lower()) if len(t) >= 3}
    return out


def _text_values(value: Any) -> list[str]:
    """Flatten DTO values to source strings without including mapping keys."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        out: list[str] = []
        for nested in value.values():
            out.extend(_text_values(nested))
        return out
    if isinstance(value, (list, tuple, set)):
        out = []
        for nested in value:
            out.extend(_text_values(nested))
        return out
    return []


def _field_texts(
    product_facts: Mapping[str, Any] | None,
    dto_field: str,
) -> list[str]:
    if not product_facts:
        return []
    if not dto_field or dto_field == "*tokens*":
        return _text_values(product_facts)
    out: list[str] = []
    for path in dto_field.split(";"):
        path = path.strip()
        if path:
            out.extend(_text_values(_dig(product_facts, path)))
    return out


def _has_unnegated_token(words: list[str], target: str) -> tuple[bool, bool]:
    """Return (positive occurrence, explicitly negated occurrence)."""
    positive = False
    negated = False
    for idx, raw_word in enumerate(words):
        word = _stem(raw_word)
        if word != target:
            for prefix in _NEGATION_PREFIXES:
                if raw_word.startswith(prefix) \
                        and _stem(raw_word[len(prefix):]) == target:
                    negated = True
                    break
            continue
        qualifier = {
            _stem(v) for v in words[max(0, idx - 5):idx]
        }
        if {"whether", "not", "or"} <= qualifier:
            # "whether or not cooked or stuffed" does not assert either
            # polarity about the product. It is a legal scope qualifier.
            continue
        lookback = {_stem(v) for v in words[max(0, idx - 3):idx]}
        if lookback & _NEGATION_WORDS:
            negated = True
        else:
            positive = True
    return positive, negated


def MatchHasTokenPolarity(
    values: set[str] | frozenset[str],
    product_facts: Mapping[str, Any] | None,
    dto_field: str,
) -> tuple[str | None, str]:
    """Resolve signed evidence for polarity-sensitive form predicates.

    ``None`` means this predicate is outside the supported polarity concept
    or has no polarity-relevant evidence, so the legacy evaluator may run.
    ``unknown`` is an explicit SILENCE result: a neutral legal qualifier,
    contradictory lanes, or an uncorroborated wrapper boolean must not fall
    through to unsigned token matching.
    """
    query_tokens = {
        _stem(token)
        for value in values
        for token in _TOKEN.findall(str(value).lower())
    }
    processing_fields = {
        path.strip().rsplit(".", 1)[-1]
        for path in dto_field.split(";")
        if path.strip()
    }
    processing_queries = query_tokens & _PROCESSING_POLARITY.keys()
    if processing_queries and "processing_state" in processing_fields:
        state_tokens = _field_tokens(product_facts, dto_field)
        positive_states: set[str] = set()
        negative_states: set[str] = set()
        for query in processing_queries:
            positives, negatives = _PROCESSING_POLARITY[query]
            positive_states.update(positives)
            negative_states.update(negatives)

        requires_cooking = (
            "requires_cooking" in state_tokens
            or {"require", "cooking"} <= state_tokens
        )
        positive = bool(state_tokens & positive_states) or (
            requires_cooking and "requires_cooking" in positive_states
        )
        negative = bool(state_tokens & negative_states) or (
            requires_cooking and "requires_cooking" in negative_states
        )
        if positive and negative:
            return "unknown", "processing_state_conflict"
        if positive:
            return "true", "processing_state_equivalent"
        if negative:
            return "false", "processing_state_opposite"
        if state_tokens:
            return "unknown", "processing_state_unresolved"
        return None, ""

    concept_terms: set[str] = set()
    for token in query_tokens:
        concept_terms.update(_FORM_POLARITY_EQUIVALENTS.get(token, ()))
    if not concept_terms:
        return None, ""

    positive = False
    negated = False
    mentioned = False
    for text in _field_texts(product_facts, dto_field):
        words = [word.lower() for word in _TOKEN.findall(text.lower())]
        stems = {_stem(word) for word in words}
        for target in concept_terms:
            target_positive, target_negated = _has_unnegated_token(
                words, target
            )
            positive = positive or target_positive
            negated = negated or target_negated
            mentioned = mentioned or target in stems or any(
                word.startswith(_NEGATION_PREFIXES)
                and _stem(word[3:] if word.startswith("non") else word[2:])
                == target
                for word in words
            )

    wrapper = _dig(product_facts, _WRAPPER_FIELD)
    wrapper_is_bound = (
        _WRAPPER_FIELD in {
            path.strip() for path in dto_field.split(";")
        }
        and isinstance(wrapper, bool)
    )

    if positive and negated:
        return "unknown", "polarity_conflict"
    if positive:
        if wrapper_is_bound and wrapper is False:
            return "unknown", "polarity_conflict:wrapper_false"
        return "true", "affirmative_source_text"
    if negated:
        if wrapper_is_bound and wrapper is True:
            return "unknown", "polarity_conflict:wrapper_true"
        return "false", "explicit_negation"
    if mentioned:
        return "unknown", "neutral_scope_qualifier"
    if wrapper_is_bound:
        return "unknown", "wrapper_requires_text_corroboration"
    return None, ""


def MatchPositivePhraseEvidence(
    phrase: set[str] | frozenset[str],
    product_facts: Mapping[str, Any] | None,
    dto_field: str,
    fallback_pool: set[str] | frozenset[str],
) -> tuple[bool, str]:
    """Verify that a token hit is affirmative and has the intended sense.

    This is used only for exclusion evidence. Explicitly negated mentions
    (``not stuffed``, ``without sauce``) and prepared-dish ``meal`` mentions
    are not treated as proof that an excluded property is present. Failure to
    prove presence remains unknown; it never becomes affirmative absence.
    """
    targets = {_stem(str(token).lower()) for token in phrase if str(token)}
    pool = {_stem(str(token).lower()) for token in fallback_pool if str(token)}
    if not targets or not targets <= pool:
        return False, "phrase_absent"

    texts = _field_texts(product_facts, dto_field)
    if not texts:
        return True, "token_pool_fallback"

    saw_negated = False
    saw_sense_guard = False
    positive_targets: set[str] = set()
    for text in texts:
        words = [word.lower() for word in _TOKEN.findall(text.lower())]
        for target in targets:
            positive, negated = _has_unnegated_token(words, target)
            saw_negated = saw_negated or negated
            if not positive:
                continue
            # CN uses "meal" both for a milling/feed product and ordinary
            # prepared meals. Only the former can satisfy a tariff exclusion.
            if target != "meal":
                positive_targets.add(target)
                continue
            stems = {_stem(word) for word in words}
            if stems & _PREPARED_MEAL_CONTEXT:
                saw_sense_guard = True
                continue
            if not stems & _MILLING_MEAL_CONTEXT:
                saw_sense_guard = True
                continue
            positive_targets.add(target)

    if targets <= positive_targets:
        return True, "affirmative_source_text"

    if saw_negated:
        return False, "explicit_negation_guarded"
    if saw_sense_guard:
        return False, "meal_sense_guarded"
    return False, "source_text_not_affirmative"


def LoadBranchPredicates(level: str, codes: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
    """{code -> [predicate rows]} from the sidecar; () -> {} on any failure."""
    if not codes:
        return {}
    try:
        from sqlalchemy import bindparam, text

        from db.db_session_manager import DbSessionManager

        manager = DbSessionManager.GetInstance()
        # A/B between compiler generations: ASAP_PREDICATE_VERSION selects
        # which sidecar generation feeds the runtime. Default rule-v2 matches
        # branch_predicate_compiler.py, so a freshly compiled sidecar is read
        # without requiring an env override. "all" stacks generations and is
        # NOT a measured/default configuration.
        version = (os.environ.get("ASAP_PREDICATE_VERSION", "rule-v2") or "").strip()
        version_clause = " AND predicate_version = :version" if version and version != "all" else ""
        params: dict[str, Any] = {"level": level, "codes": tuple(codes)}
        if version_clause:
            params["version"] = version
        rows = manager.FetchRows(
            text(
                'SELECT code, axis, dto_field, op, value, severity, compile_status'
                ' FROM "branch_predicate_index"'
                " WHERE level = :level AND code IN :codes" + version_clause
            ).bindparams(bindparam("codes", expanding=True)),
            params,
        )
    except Exception:  # noqa: BLE001 — sidecar absent = layer off
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        data = dict(row)
        if str(data.get("compile_status") or "") != "active":
            continue
        out.setdefault(str(data.get("code") or ""), []).append(data)
    return out


def EvaluatePredicates(
    predicates: list[Mapping[str, Any]],
    fact_tokens: frozenset[str] | set[str],
    product_facts: Mapping[str, Any] | None = None,
    *,
    closed_world: bool = False,
    allow_pool: bool = True,
) -> tuple[float, list[dict[str, str]]]:
    """(score delta, per-predicate verdicts) — three-valued, GRADED.

    has_token axes evaluate the bound DTO field first (strong true, +3);
    when the field is silent the broad token pool may still answer (weak
    true, +1); otherwise unknown (0). Violations (not_contains) stay on the
    pool — a violation must be caught wherever the token lives.
    """
    delta = 0.0
    verdicts: list[dict[str, str]] = []
    pool = set(fact_tokens)
    # 잔존하는 유일한 broad-pool 지원 경로(true_pool +1)의 A/B 스위치.
    # 기본 ON(73% 실측 구성). OFF면 has_token은 바인딩 필드로만 답한다.
    pool_boost_on = (
        allow_pool
        and (os.environ.get("ASAP_PREDICATE_POOL_BOOST", "1") or "1").strip()
        != "0"
    )
    for pred in predicates:
        axis = str(pred.get("axis") or "")
        if axis in _SKIP_AXES:
            continue
        try:
            values = json.loads(str(pred.get("value") or "null"))
        except Exception:  # noqa: BLE001
            values = None
        tokens = {str(v).strip().lower() for v in (values or []) if str(v).strip()}
        if not tokens:
            continue
        op = str(pred.get("op") or "")
        # [9회차 [2]] 교차참조 배제절 negation은 미결 — 'other than
        # pencils of heading 9608'은 코드 간 경계 참조지 상품 부정이
        # 아니다 (9609가 자기 정체어 pencil로 -100 되던 실측 — 컴파일러
        # ·staged와 동일 문법 규칙의 평가기판. 재컴파일 없이 기존
        # rule-v2 행에도 적용).
        if op == "not_contains" and re.search(
                r"\bof\s+(?:heading|chapter)s?\b",
                str(pred.get("source_text") or ""), re.I):
            verdicts.append({"axis": axis, "op": op,
                             "value": ",".join(sorted(tokens))[:60],
                             "verdict": "unknown",
                             "why": "crossref_exclusion_skipped"})
            continue
        if op == "not_contains":
            dto_field = str(pred.get("dto_field") or "*tokens*")
            phrases = [
                {_stem(t) for t in _TOKEN.findall(str(value).lower()) if len(t) >= 3}
                for value in (values or [])
            ]
            matches = [
                MatchPositivePhraseEvidence(
                    phrase, product_facts,
                    dto_field, pool,
                )
                for phrase in phrases if phrase
            ]
            hit = any(matched for matched, _ in matches)
            explicit_negative = bool(matches) and all(
                reason == "explicit_negation_guarded"
                for matched, reason in matches
                if not matched
            ) and not any(matched for matched, _ in matches)
            if hit:
                verdict = "false"
            elif explicit_negative:
                verdict = "true"
            else:
                verdict = "unknown"
            if hit:
                delta -= _PREDICATE_BLOCK
            elif verdict == "true":
                delta += _FIELD_BOOST
        elif op == "has_token":
            dto_field = str(pred.get("dto_field") or "")
            polarity_verdict, polarity_why = MatchHasTokenPolarity(
                tokens,
                product_facts,
                dto_field,
            )
            if polarity_verdict is not None:
                verdict = polarity_verdict
                if verdict == "true":
                    delta += _FIELD_BOOST
                elif verdict == "false":
                    delta -= _PREDICATE_BLOCK
            else:
                bound = _field_tokens(product_facts, dto_field)
                if axis in _ALIAS_AXES:
                    bound = _expand(bound)
                if tokens & bound:
                    verdict = "true"
                    delta += _FIELD_BOOST
                elif pool_boost_on and axis not in _STRICT_FIELD_AXES and tokens & (
                    _expand(pool) if axis in _ALIAS_AXES else pool
                ):
                    verdict = "true_pool"
                    delta += _POOL_BOOST
                elif closed_world and bound:
                    verdict = "false"
                    delta -= _PREDICATE_BLOCK
                else:
                    verdict = "unknown"
        else:
            verdict = "unknown"
        why = ""
        if op == "not_contains" and not hit:
            if verdict == "true":
                why = "explicit_negation"
            else:
                guarded = [reason for matched, reason in matches
                           if not matched and reason not in {"phrase_absent"}]
                why = guarded[0] if guarded else "exclusion_absent"
        if op == "has_token":
            if polarity_verdict is not None:
                why = polarity_why
            elif verdict in {"unknown", "false"}:
                bound_probe = _field_tokens(
                    product_facts, str(pred.get("dto_field") or "")
                )
                why = (
                    "field_empty"
                    if not bound_probe
                    else (
                        "canonical_field_mismatch"
                        if verdict == "false"
                        else "field_no_match"
                    )
                )
        result = {
            "axis": axis,
            "op": op,
            "value": ",".join(sorted(tokens))[:60],
            "verdict": verdict,
            "why": why,
        }
        if op == "has_token" and polarity_verdict is not None:
            # A signed fact answers a narrower legal question than a broad
            # axis token match. The later HS4/HS6 axis stamp must therefore
            # consume this result before considering a processing/form axis.
            # This contract is concept-generic; MatchHasTokenPolarity owns the
            # supported signed vocabulary.
            result["authority"] = "signed_polarity"
            result["decisive"] = "true"
        verdicts.append(result)
    return delta, verdicts

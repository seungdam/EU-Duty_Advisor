"""Stage-aware HS4 -> HS6 -> CN8 classifier tool.

Owned by Classification_Agent.  The tool is intentionally a pure callable:
it reads ProductFacts_dto / Route_dto-shaped dictionaries, reads ``cn_table``
through the existing Supabase-only llm_classifier adapter, and returns compact
stage payloads.  Classification_Agent owns all blackboard writes.

The important behavior is *not* a projection from one CN8 result.  Each stage
builds its own candidate slice:

  Route_dto chapter candidates -> HS4 candidate rows
  retained HS4                -> HS6 candidate rows
  retained HS6                -> CN8 candidate rows

The same constrained LLM caller used by ``agents.llm_classifier`` is reused as
a stage selector over already-sliced candidates.  If the LLM response is not
parseable, the tool falls back to deterministic lexical/embedding rank and
records that in the stage audit.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from typing import Any, Iterable


TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")


def _compact(values: Iterable[Any], *, limit: int = 40) -> list[Any]:
    out: list[Any] = []
    for value in values:
        if value in (None, "", [], {}):
            continue
        if value not in out:
            out.append(value)
        if len(out) >= limit:
            break
    return out


def _as_text(value: Any, *, limit: int = 4000) -> str:
    if value in (None, "", [], {}):
        return ""
    text = value if isinstance(value, str) else str(value)
    return text.strip()[:limit]


def _digits(value: Any, *, limit: int = 99) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:limit]


def _tokens(text: str) -> set[str]:
    return {tok.lower() for tok in TOKEN_RE.findall(text or "") if len(tok) >= 2}


def _read(row: dict[str, Any], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _row_cn8(row: dict[str, Any]) -> str:
    return _digits(_read(row, "cn", "cn8", "hs8"), limit=8)


def _row_hs6(row: dict[str, Any]) -> str:
    code = _digits(_read(row, "subheading", "hs6_code", "hs6"), limit=6)
    return code if len(code) == 6 else _row_cn8(row)[:6]


def _row_hs4(row: dict[str, Any]) -> str:
    code = _digits(_read(row, "heading", "hs4_code", "hs4"), limit=4)
    return code if len(code) == 4 else _row_cn8(row)[:4]


def _row_chapter(row: dict[str, Any]) -> str:
    chapter = _digits(row.get("chapter"), limit=2)
    if chapter:
        return chapter.zfill(2)
    return _row_cn8(row)[:2]


def _row_full_description(row: dict[str, Any]) -> str:
    parts = [
        _read(row, "heading_description"),
        _read(row, "subheading_description"),
        _read(row, "branch_context"),
        _read(row, "cn_description"),
    ]
    return " | ".join(part for part in parts if part)


def _row_search_text(row: dict[str, Any]) -> str:
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


class StagedClassificationTool:
    """Build HS4, HS6, and CN8 classification stage payloads."""

    tool_name = "StagedClassificationTool"

    def classify(
        self,
        *,
        product_evidence: dict[str, Any],
        product_facts: dict[str, Any],
        routing_context: dict[str, Any],
        top_k: int = 8,
    ) -> dict[str, Any]:
        from agents import llm_classifier

        product_input = self._build_product_input(product_evidence, product_facts)
        sanitized_input, removed_allergens = llm_classifier._sanitize_classification_input(product_input)
        expanded_input = llm_classifier._expand_query(sanitized_input)
        route_chapters = self._route_chapters(routing_context)
        strict_route = os.environ.get("ASAP_STAGED_ROUTE_STRICT", "1") != "0"
        if not sanitized_input.strip():
            return self._empty("empty_product_input", product_input, route_chapters)

        all_rows = list(llm_classifier._load_cn_rows())
        stage_traces: dict[str, Any] = {}

        hs4_candidates = self._rank_stage_candidates(
            rows=all_rows,
            stage="hs4",
            expanded_input=expanded_input,
            route_chapters=route_chapters,
            strict_route=strict_route,
            parent_codes=[],
            limit=int(os.environ.get("ASAP_STAGED_HS4_POOL", "12")),
        )
        hs4_selected = self._select_stage(
            stage="hs4",
            product_input=sanitized_input,
            expanded_input=expanded_input,
            candidates=hs4_candidates,
            retain=int(os.environ.get("ASAP_STAGED_HS4_RETAIN", "3")),
        )
        stage_traces["hs4"] = hs4_selected["trace"]

        hs6_candidates = self._rank_stage_candidates(
            rows=all_rows,
            stage="hs6",
            expanded_input=expanded_input,
            route_chapters=route_chapters,
            strict_route=strict_route,
            parent_codes=hs4_selected["selected_codes"],
            limit=int(os.environ.get("ASAP_STAGED_HS6_POOL", "18")),
        )
        hs6_selected = self._select_stage(
            stage="hs6",
            product_input=sanitized_input,
            expanded_input=expanded_input,
            candidates=hs6_candidates,
            retain=int(os.environ.get("ASAP_STAGED_HS6_RETAIN", "4")),
        )
        stage_traces["hs6"] = hs6_selected["trace"]

        cn8_candidates = self._rank_stage_candidates(
            rows=all_rows,
            stage="cn8",
            expanded_input=expanded_input,
            route_chapters=route_chapters,
            strict_route=strict_route,
            parent_codes=hs6_selected["selected_codes"],
            limit=max(int(os.environ.get("ASAP_STAGED_CN8_POOL", "24")), top_k),
        )
        cn8_selected = self._select_stage(
            stage="cn8",
            product_input=sanitized_input,
            expanded_input=expanded_input,
            candidates=cn8_candidates,
            retain=top_k,
        )
        stage_traces["cn8"] = cn8_selected["trace"]

        stage_payloads = self._build_stage_payloads(
            product_facts=product_facts,
            routing_context=routing_context,
            product_input=product_input,
            sanitized_input=sanitized_input,
            route_chapters=route_chapters,
            removed_allergens=removed_allergens,
            strict_route=strict_route,
            stages={
                "hs4": hs4_selected,
                "hs6": hs6_selected,
                "cn8": cn8_selected,
            },
        )
        final_candidates = self._to_classifier_rows(cn8_selected["retained_candidates"], top_k=top_k)
        return {
            "ok": bool(final_candidates),
            "error": "" if final_candidates else "no_cn8_candidates",
            "product_input": product_input,
            "sanitized_input": sanitized_input,
            "chapter_hint": ",".join(route_chapters),
            "route_chapters": route_chapters,
            "candidates": final_candidates,
            "stage_payloads": stage_payloads,
            "trace": {
                "provider": getattr(llm_classifier, "LLM_PROVIDER", ""),
                "model": getattr(llm_classifier, "LLM_MODEL", ""),
                "stage_traces": stage_traces,
                "removed_allergen_count": len(removed_allergens),
                "removed_allergen_fragments": removed_allergens[:80],
                "strict_route": strict_route,
                "parse_ok": all(stage_traces.get(stage, {}).get("parse_ok") for stage in ("hs4", "hs6", "cn8")),
                "fallback_used": any(stage_traces.get(stage, {}).get("fallback_used") for stage in ("hs4", "hs6", "cn8")),
            },
        }

    def _empty(self, error: str, product_input: str, route_chapters: list[str]) -> dict[str, Any]:
        return {
            "ok": False,
            "error": error,
            "product_input": product_input,
            "sanitized_input": "",
            "chapter_hint": ",".join(route_chapters),
            "route_chapters": route_chapters,
            "candidates": [],
            "stage_payloads": {},
            "trace": {"error": error},
        }

    def _route_chapters(self, routing_context: dict[str, Any]) -> list[str]:
        chapters = []
        for item in routing_context.get("candidate_chapters") or []:
            if not isinstance(item, dict):
                continue
            chapter = _digits(item.get("chapter") or item.get("chapter_code"), limit=2)
            if chapter:
                chapters.append(chapter.zfill(2))
        return _compact(chapters, limit=8)

    def _build_product_input(
        self,
        product_evidence: dict[str, Any],
        product_facts: dict[str, Any],
    ) -> str:
        obs = product_evidence.get("observed_facts") or {}
        projection = product_facts.get("classifier_projection") if isinstance(product_facts.get("classifier_projection"), dict) else {}
        identity_lane = product_facts.get("identity_lane") if isinstance(product_facts.get("identity_lane"), dict) else {}
        composition_lane = product_facts.get("composition_lane") if isinstance(product_facts.get("composition_lane"), dict) else {}
        distilled = identity_lane.get("distilled_identity") if isinstance(identity_lane.get("distilled_identity"), dict) else {}

        parts = [
            f"product_name: {_as_text(obs.get('product_name') or product_facts.get('product_name'), limit=500)}",
            f"description: {_as_text(obs.get('description') or product_facts.get('short_description'), limit=1000)}",
            f"normalized_tariff_description: {_as_text(distilled.get('normalized_tariff_description') or product_facts.get('normalized_tariff_description'), limit=700)}",
            f"ingredient_class: {_as_text(distilled.get('ingredient_class'), limit=100)}",
            f"food_form: {_as_text(distilled.get('food_form'), limit=100)}",
            f"processing_state: {_as_text(distilled.get('processing_state') or composition_lane.get('processing_state') or product_facts.get('processing_state'), limit=100)}",
            f"identity_context:\n{_as_text(projection.get('identity_context'), limit=1800)}",
            f"composition_context:\n{_as_text(projection.get('composition_context'), limit=2200)}",
            f"classification_text_en:\n{_as_text(product_facts.get('classification_text_en'), limit=1800)}",
            f"classification_text:\n{_as_text(product_facts.get('classification_text'), limit=2200)}",
        ]
        composition = obs.get("composition")
        if isinstance(composition, list) and composition:
            parts.append("observed_composition:\n" + "\n".join(_as_text(x, limit=300) for x in composition[:30]))
        return "\n".join(part for part in parts if part and not part.endswith(": "))

    def _row_allowed_for_stage(
        self,
        row: dict[str, Any],
        *,
        stage: str,
        route_chapters: list[str],
        strict_route: bool,
        parent_codes: list[str],
    ) -> bool:
        cn8 = _row_cn8(row)
        if len(cn8) != 8:
            return False
        if route_chapters and strict_route and _row_chapter(row) not in set(route_chapters):
            return False
        if stage == "hs6" and parent_codes:
            return _row_hs4(row) in set(parent_codes)
        if stage == "cn8" and parent_codes:
            return _row_hs6(row) in set(parent_codes)
        return True

    def _group_stage_rows(self, rows: list[dict[str, Any]], stage: str) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            code = _row_hs4(row) if stage == "hs4" else _row_hs6(row) if stage == "hs6" else _row_cn8(row)
            if code:
                grouped[code].append(row)

        candidates: list[dict[str, Any]] = []
        for code, group_rows in grouped.items():
            first = group_rows[0]
            if stage == "hs4":
                description = _read(first, "heading_description") or _row_full_description(first)
                rule_text = "\n".join(_compact([
                    _read(first, "heading_including"),
                    _read(first, "heading_excluding"),
                    _read(first, "heading_keywords"),
                    *(_read(r, "branch_context") for r in group_rows[:8]),
                    *(_read(r, "subheading_description") for r in group_rows[:8]),
                ], limit=18))
            elif stage == "hs6":
                description = _read(first, "subheading_description") or _row_full_description(first)
                rule_text = "\n".join(_compact([
                    _read(first, "heading_description"),
                    _read(first, "subheading_description"),
                    _read(first, "subheading_keywords"),
                    *(_read(r, "branch_context") for r in group_rows[:8]),
                    *(_read(r, "cn_description") for r in group_rows[:8]),
                ], limit=18))
            else:
                description = _row_full_description(first)
                rule_text = _row_search_text(first)
            sample_cn8 = _compact((_row_cn8(r) for r in group_rows), limit=10)
            candidates.append({
                "code": code,
                "stage": stage,
                "chapter": code[:2],
                "parent_code": code[:4] if stage == "hs6" else code[:6] if stage == "cn8" else code[:2],
                "description": description,
                "rule_context": rule_text[:1600],
                "row_count": len(group_rows),
                "sample_cn8": sample_cn8,
                "source_id": code,
                "source_table": "cn_table",
            })
        return candidates

    def _rank_stage_candidates(
        self,
        *,
        rows: list[dict[str, Any]],
        stage: str,
        expanded_input: str,
        route_chapters: list[str],
        strict_route: bool,
        parent_codes: list[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        allowed = [
            row
            for row in rows
            if self._row_allowed_for_stage(
                row,
                stage=stage,
                route_chapters=route_chapters,
                strict_route=strict_route,
                parent_codes=parent_codes,
            )
        ]
        if not allowed and strict_route:
            allowed = [
                row
                for row in rows
                if self._row_allowed_for_stage(
                    row,
                    stage=stage,
                    route_chapters=[],
                    strict_route=False,
                    parent_codes=parent_codes,
                )
            ]

        candidates = self._group_stage_rows(allowed, stage)
        query_tokens = _tokens(expanded_input)
        scored: list[dict[str, Any]] = []
        for cand in candidates:
            text = f"{cand['description']}\n{cand['rule_context']}"
            tokens = _tokens(text)
            overlap = len(query_tokens & tokens)
            exact_bonus = 0.0
            lower_query = expanded_input.lower()
            lower_text = text.lower()
            for term in (
                "soup", "broth", "stew", "noodle", "pasta", "dumpling", "shrimp",
                "prawn", "octopus", "squid", "cockle", "mollusc", "pork", "beef",
                "chicken", "sauce", "seasoning", "cosmetic", "skin", "deodorant",
            ):
                if term in lower_query and term in lower_text:
                    exact_bonus += 3.0
            route_bonus = 2.0 if route_chapters and cand["chapter"] in set(route_chapters) else 0.0
            score = float(overlap) + exact_bonus + route_bonus
            if score <= 0 and stage != "cn8":
                score = route_bonus
            if score > 0:
                cand = dict(cand)
                cand["score"] = round(score, 4)
                scored.append(cand)
        if not scored:
            scored = [dict(cand, score=0.0) for cand in candidates]

        scored.sort(key=lambda item: (-float(item.get("score") or 0.0), item["code"]))
        preselected = scored[: max(limit * 3, limit)]

        if os.environ.get("ASAP_STAGED_USE_EMBEDDING", "0") == "1" and preselected:
            preselected = self._rerank_with_embedding(expanded_input, preselected)

        return preselected[:limit]

    def _rerank_with_embedding(self, expanded_input: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from agents import llm_classifier

        try:
            query_vec = llm_classifier._embed([expanded_input])[0]
            cand_vecs = llm_classifier._embed([
                f"{cand.get('description')}\n{cand.get('rule_context')}"[:2000]
                for cand in candidates
            ])
        except Exception as exc:  # noqa: BLE001
            return [dict(cand, embedding_error=str(exc)[:200]) for cand in candidates]

        reranked: list[dict[str, Any]] = []
        for cand, vec in zip(candidates, cand_vecs):
            semantic = llm_classifier._cosine(query_vec, vec)
            combined = float(cand.get("score") or 0.0) + semantic * 8.0
            reranked.append({
                **cand,
                "semantic_similarity": round(float(semantic), 4),
                "score": round(combined, 4),
            })
        reranked.sort(key=lambda item: (-float(item.get("score") or 0.0), item["code"]))
        return reranked

    def _select_stage(
        self,
        *,
        stage: str,
        product_input: str,
        expanded_input: str,
        candidates: list[dict[str, Any]],
        retain: int,
    ) -> dict[str, Any]:
        from agents import llm_classifier

        candidate_payload = [
            {
                "code": cand["code"],
                "description": _as_text(cand.get("description"), limit=150),
                "context": _as_text(cand.get("rule_context"), limit=120),
                "score": cand.get("score"),
            }
            for cand in candidates
        ]
        trace = {
            "stage": stage,
            "candidate_count": len(candidates),
            "candidate_payload": candidate_payload,
            "parse_ok": False,
            "fallback_used": False,
            "raw_response": "",
            "prompt": "",
        }
        selected: list[dict[str, Any]] = []
        if candidates and os.environ.get("ASAP_STAGED_LLM_PER_STAGE", "1") != "0":
            prompt = "\n".join([
                f"Select EU CN {stage.upper()} prefix codes from STAGE_CANDIDATES only.",
                f"Return JSON only with shape: {{\"codes\":[\"code\"]}}. Maximum {retain} codes.",
                "",
                "PRODUCT_EVIDENCE:",
                expanded_input[:700],
                "",
                "STAGE_CANDIDATES:",
                json.dumps(candidate_payload, ensure_ascii=False),
            ])
            raw = llm_classifier.llm(prompt, max_tokens=int(os.environ.get("ASAP_STAGED_LLM_MAX_TOKENS", "768")))
            parsed = llm_classifier.parse_json(raw)
            trace.update({
                "prompt": prompt,
                "prompt_length": len(prompt),
                "raw_response": raw,
                "raw_response_length": len(raw),
                "parse_ok": isinstance(parsed.get("codes"), list) or isinstance(parsed.get("ranked"), list),
            })
            by_code = {str(cand["code"]): cand for cand in candidates}
            parsed_codes: list[Any] = []
            if isinstance(parsed.get("codes"), list):
                parsed_codes = parsed["codes"]
            elif isinstance(parsed.get("ranked"), list):
                parsed_codes = [
                    item.get("code")
                    for item in parsed["ranked"]
                    if isinstance(item, dict)
                ]
            for raw_code in parsed_codes:
                code = _digits(raw_code, limit=8)
                cand = by_code.get(code)
                if cand:
                    selected.append({
                        **cand,
                        "confidence": self._confidence(cand.get("score"), default=0.5),
                        "llm_match": True,
                        "reason": self._stage_reason(stage, cand, source="stage_llm_code_selection"),
                        "selection_source": "stage_llm_code_selection",
                    })
                    if len(selected) >= retain:
                        break
            if not selected and isinstance(parsed.get("ranked"), list):
                for item in parsed["ranked"]:
                    if isinstance(item, dict):
                        code = _digits(item.get("code"), limit=8)
                    else:
                        code = _digits(item, limit=8)
                    cand = by_code.get(code)
                    if not cand:
                        continue
                    selected.append({
                        **cand,
                        "confidence": self._confidence(cand.get("score"), default=0.5),
                        "llm_match": True,
                        "reason": self._stage_reason(stage, cand, source="stage_llm_ranked_compat"),
                        "selection_source": "stage_llm_ranked_compat",
                    })
                    if len(selected) >= retain:
                        break

        if not selected:
            trace["fallback_used"] = True
            for cand in candidates[:retain]:
                selected.append({
                    **cand,
                    "confidence": self._confidence(cand.get("score"), default=0.3),
                    "llm_match": True,
                    "reason": self._stage_reason(stage, cand, source="stage_rank_fallback"),
                    "selection_source": "stage_rank_fallback",
                })

        selected_codes = _compact((item["code"] for item in selected), limit=retain)
        reviews = []
        selected_set = set(selected_codes)
        selected_reason = {item["code"]: item.get("reason") for item in selected}
        for cand in candidates:
            code = cand["code"]
            reviews.append({
                "code": code,
                "status": "retained" if code in selected_set else "rejected_or_lower_ranked",
                "description": _as_text(cand.get("description"), limit=700),
                "score": cand.get("score"),
                "row_count": cand.get("row_count"),
                "sample_cn8": cand.get("sample_cn8") or [],
                "reason": selected_reason.get(code, ""),
                "source_table": "cn_table",
                "source_id": code,
            })
        return {
            "stage": stage,
            "candidate_pool": candidates,
            "candidate_reviews": reviews,
            "retained_candidates": selected,
            "selected_codes": selected_codes,
            "trace": trace,
        }

    def _stage_reason(self, stage: str, cand: dict[str, Any], *, source: str) -> str:
        description = _as_text(cand.get("description"), limit=160)
        score = cand.get("score")
        return (
            f"{stage.upper()} retained by {source}; code={cand.get('code')}, "
            f"score={score}, description={description}"
        )

    def _confidence(self, value: Any, *, default: Any = 0.3) -> float:
        try:
            raw = float(value if value is not None else default)
        except (TypeError, ValueError):
            raw = 0.3
        if raw > 1.0:
            raw = raw / max(raw, 10.0)
        return round(max(0.0, min(1.0, raw)), 3)

    def _decision_axes(
        self,
        *,
        product_facts: dict[str, Any],
        routing_context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        identity_lane = product_facts.get("identity_lane") if isinstance(product_facts.get("identity_lane"), dict) else {}
        composition_lane = product_facts.get("composition_lane") if isinstance(product_facts.get("composition_lane"), dict) else {}
        distilled = identity_lane.get("distilled_identity") if isinstance(identity_lane.get("distilled_identity"), dict) else {}
        chapters = [
            c.get("chapter")
            for c in routing_context.get("candidate_chapters") or []
            if isinstance(c, dict) and c.get("chapter")
        ]
        return [
            {
                "axis": "ingredient_taxonomy",
                "fields_read": [
                    "identity_lane.distilled_identity.ingredient_class",
                    "composition_lane.principal_ingredient_terms",
                    "composition_lane.ingredient_taxonomy_terms",
                    "normalized_tariff_description",
                ],
                "values": _compact([
                    distilled.get("ingredient_class"),
                    *composition_lane.get("principal_ingredient_terms", [])[:12],
                    *composition_lane.get("ingredient_taxonomy_terms", [])[:12],
                ]),
            },
            {
                "axis": "product_form",
                "fields_read": [
                    "identity_lane.distilled_identity.food_form",
                    "identity_lane.product_form_terms",
                    "classifier_projection.identity_context",
                ],
                "values": _compact([
                    distilled.get("food_form"),
                    *identity_lane.get("product_form_terms", [])[:12],
                ]),
            },
            {
                "axis": "processing_state",
                "fields_read": [
                    "identity_lane.distilled_identity.processing_state",
                    "composition_lane.processing_state",
                    "composition_lane.processing_terms",
                    "processing_signals",
                ],
                "values": _compact([
                    distilled.get("processing_state"),
                    composition_lane.get("processing_state"),
                    *composition_lane.get("processing_terms", [])[:12],
                ]),
            },
            {
                "axis": "route_chapter",
                "fields_read": [
                    "Route_dto.candidate_chapters",
                    "Route_dto.blocked_chapters",
                ],
                "values": _compact(chapters),
            },
        ]

    def _build_stage_payloads(
        self,
        *,
        product_facts: dict[str, Any],
        routing_context: dict[str, Any],
        product_input: str,
        sanitized_input: str,
        route_chapters: list[str],
        removed_allergens: list[str],
        strict_route: bool,
        stages: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        decision_axes = self._decision_axes(product_facts=product_facts, routing_context=routing_context)
        payloads: dict[str, dict[str, Any]] = {}
        next_map = {"hs4": "HS6Classify_dto", "hs6": "CN8Classify_dto", "cn8": "CodeSet_dto"}
        for stage, result in stages.items():
            retained = result.get("retained_candidates") or []
            reviews = result.get("candidate_reviews") or []
            selected_codes = result.get("selected_codes") or []
            rejected = [review for review in reviews if review.get("status") != "retained"]
            table_reads = [
                {
                    "source_table": "cn_table",
                    "source_id": str(review.get("source_id") or review.get("code") or ""),
                    "snippet": _as_text(review.get("description"), limit=300),
                    "reason": f"{stage.upper()} stage candidate reviewed by StagedClassificationTool.",
                }
                for review in reviews[:20]
                if review.get("source_id") or review.get("code")
            ]
            payloads[stage] = {
                "classifier_engine": "staged_llm_classifier.controller_driven",
                "query_used": {
                    "product_input_excerpt": product_input[:2500],
                    "sanitized_input_excerpt": sanitized_input[:2500],
                    "route_chapters": route_chapters,
                    "strict_route": strict_route,
                    "stage": stage,
                    "stage_candidate_count": len(result.get("candidate_pool") or []),
                },
                "table_reads": table_reads,
                "decision_axes": decision_axes,
                "candidate_reviews": reviews,
                "retained_candidates": [
                    {
                        "code": item.get("code"),
                        "description": _as_text(item.get("description"), limit=700),
                        "confidence": item.get("confidence"),
                        "reason": item.get("reason"),
                        "selection_source": item.get("selection_source"),
                        "sample_cn8": item.get("sample_cn8") or [],
                        "row_count": item.get("row_count"),
                    }
                    for item in retained
                ],
                "rejected_candidates": rejected,
                "selected_codes": selected_codes,
                "missing_facts": [],
                "next_stage": next_map[stage],
                "audit": {
                    "stage_trace": result.get("trace") or {},
                    "removed_allergen_count": len(removed_allergens),
                    "removed_allergen_fragments": removed_allergens[:30],
                },
            }
        return payloads

    def _to_classifier_rows(self, retained_cn8: list[dict[str, Any]], *, top_k: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in retained_cn8[:top_k]:
            cn8 = _digits(item.get("code"), limit=8)
            if len(cn8) != 8:
                continue
            rows.append({
                "no": len(rows) + 1,
                "결정세번": cn8,
                "hs6": cn8[:6],
                "물품설명_원문": _as_text(item.get("description"), limit=1000),
                "물품설명_한글": "",
                "분류사유_영문": _as_text(item.get("reason"), limit=500),
                "분류사유_한글": "",
                "유사도_nom": item.get("score"),
                "신뢰도": self._confidence(item.get("confidence"), default=0.3),
                "검증_상태": "확정" if item.get("llm_match") is not False else "재검토필요",
                "분류_단계": "hs4_hs6_cn8_staged",
                "bti_참조": "",
                "bti_국가": "",
                "bti_유사도": None,
                "적용법령": "",
                "cn_note_있음": bool(item.get("rule_context")),
            })
        return rows

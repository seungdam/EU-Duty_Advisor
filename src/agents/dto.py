"""Shared DTO builders for blackboard agent handoffs.

The builders keep object shapes visible without introducing Pydantic/dataclass
contracts too early. Runtime slot names stay backward-compatible for now; the
``dto_name`` field records the shorter DTO contract name we are moving toward.
"""
from __future__ import annotations

from typing import Any


def ProductFacts_dto(
    *,
    created_by: str,
    created_at: str,
    understanding_id: str,
    product_id: str,
    product_name: str,
    short_description: str,
    classification_text: str,
    classification_text_en: str,
    translated_product_name: str,
    commercial_identity: str,
    normalized_tariff_description: str,
    classification_text_line_count: int,
    keywords: list[str],
    keyword_map: dict[str, list[str]],
    product_form_terms: list[str],
    processing_terms: list[str],
    principal_ingredient_terms: list[str],
    ingredient_taxonomy_terms: list[str],
    composition_terms: list[str],
    use_context_terms: list[str],
    chapter_routing_terms: list[str],
    routing_keywords: list[str],
    blocked_routing_terms: list[dict[str, Any]],
    excluded_from_routing_terms: list[dict[str, Any]],
    allergen_notice_texts: list[str],
    allergen_notice_terms: list[str],
    processing_state: str,
    processing_signals: list[dict[str, Any]],
    raw_material_signals: list[dict[str, Any]],
    domain_hints: list[str],
    product_understanding_mode: str,
    llm_understanding_error: str,
    llm_confidence: Any,
    needs_review: bool,
    routing_terms: list[str],
    unknowns: list[str],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Output answer shape for ProductUnderstandingAgent.

    Blackboard compatibility:
    - object_type remains ``ProductUnderstandingFacts`` for the existing store.
    - dto_name exposes the shorter contract name for new agents/docs.
    """
    return {
        "object_type": "ProductUnderstandingFacts",
        "dto_name": "ProductFacts_dto",
        "created_by": created_by,
        "created_at": created_at,
        "understanding_id": understanding_id,
        "product_id": product_id,
        "source_product_id": product_id,
        "product_name": product_name,
        "short_description": short_description,
        "classification_text": classification_text,
        "classification_text_en": classification_text_en,
        "translated_product_name": translated_product_name,
        "commercial_identity": commercial_identity,
        "normalized_tariff_description": normalized_tariff_description,
        "classification_text_line_count": classification_text_line_count,
        "keywords": keywords,
        "keyword_map": keyword_map,
        "product_form_terms": product_form_terms,
        "processing_terms": processing_terms,
        "principal_ingredient_terms": principal_ingredient_terms,
        "ingredient_taxonomy_terms": ingredient_taxonomy_terms,
        "composition_terms": composition_terms,
        "use_context_terms": use_context_terms,
        "chapter_routing_terms": chapter_routing_terms,
        "routing_keywords": routing_keywords,
        "blocked_routing_terms": blocked_routing_terms,
        "excluded_from_routing_terms": excluded_from_routing_terms,
        "allergen_notice_texts": allergen_notice_texts,
        "allergen_notice_terms": allergen_notice_terms,
        "processing_state": processing_state,
        "processing_signals": processing_signals,
        "raw_material_signals": raw_material_signals,
        "domain_hints": domain_hints,
        "product_understanding_mode": product_understanding_mode,
        "llm_understanding_error": llm_understanding_error,
        "llm_confidence": llm_confidence,
        "needs_review": needs_review,
        "routing_terms": routing_terms,
        "unknowns": [u for u in unknowns if u],
        "evidence": evidence,
    }


def Route_dto(
    *,
    created_by: str,
    created_at: str,
    routing_context_id: str,
    product_id: str,
    source_product_facts_id: str,
    candidate_chapters: list[dict[str, Any]],
    blocked_chapters: list[dict[str, Any]],
    domain_scopes: list[str],
    pre_gate_domains: list[str],
    processing_state: str,
    routing_basis: dict[str, Any],
    missing_facts: list[str],
    evidence: list[dict[str, Any]],
    classifier_lane: dict[str, Any],
    document_lane: dict[str, Any],
    soft_filter: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    """Output answer shape for DomainRouterAgent.

    It is the fan-out contract: one lane feeds CN/HS classification, the other
    feeds baseline/pre-TARIC document preparation. Legacy field names are kept
    because ClassificationAgent and DocumentAgent already consume them.
    """
    return {
        "object_type": "RoutingContext",
        "dto_name": "Route_dto",
        "created_by": created_by,
        "created_at": created_at,
        "routing_context_id": routing_context_id,
        "product_id": product_id,
        "source_understanding_id": source_product_facts_id,
        "source_product_facts_id": source_product_facts_id,
        "candidate_chapters": candidate_chapters,
        "blocked_chapters": blocked_chapters,
        "domain_scopes": domain_scopes,
        "pre_gate_domains": pre_gate_domains,
        "processing_state": processing_state,
        "soft_filter": soft_filter,
        "routing_basis": routing_basis,
        "missing_facts": missing_facts,
        "evidence": evidence,
        "classifier_lane": classifier_lane,
        "document_lane": document_lane,
        **extra,
    }


def Classify_dto(
    *,
    created_by: str,
    created_at: str,
    classify_result_id: str,
    product_id: str,
    source_product_facts_id: str,
    source_routing_context_id: str,
    classifier_engine: str,
    query_used: dict[str, Any],
    table_reads: list[dict[str, Any]],
    candidate_reviews: list[dict[str, Any]],
    retained_candidates: list[dict[str, Any]],
    rejected_candidates: list[dict[str, Any]],
    classification_basis: list[str],
    citations: list[dict[str, Any]],
    missing_facts: list[str],
    audit: dict[str, Any],
    bti_lookup_requests: list[dict[str, Any]] | None = None,
    confidence_summary: dict[str, Any] | None = None,
    next_dto: str = "CodeSet_dto",
    **extra: Any,
) -> dict[str, Any]:
    """Output answer shape for ClassificationAgent.

    The classifier *reads* ProductFacts_dto + Route_dto as its inputs. This DTO
    records the search/review result that should be converted into CodeSet_dto
    and then handed to TARIC branch resolution.
    """
    return {
        "object_type": "ClassificationResult",
        "dto_name": "Classify_dto",
        "created_by": created_by,
        "created_at": created_at,
        "classify_result_id": classify_result_id,
        "product_id": product_id,
        "source_product_facts_id": source_product_facts_id,
        "source_routing_context_id": source_routing_context_id,
        "classifier_engine": classifier_engine,
        "query_used": query_used,
        "table_reads": table_reads,
        "candidate_reviews": candidate_reviews,
        "retained_candidates": retained_candidates,
        "rejected_candidates": rejected_candidates,
        "classification_basis": classification_basis,
        "citations": citations,
        "missing_facts": missing_facts,
        "bti_lookup_requests": bti_lookup_requests or [],
        "confidence_summary": confidence_summary or {},
        "next_dto": next_dto,
        "audit": audit,
        **extra,
    }


def CodeSet_dto(**values: Any) -> dict[str, Any]:
    return {"object_type": "CandidateCodeSet", "dto_name": "CodeSet_dto", **values}


def TaricSet_dto(**values: Any) -> dict[str, Any]:
    return {"object_type": "TaricSet", "dto_name": "TaricSet_dto", **values}


def DocBaseSet_dto(
    *,
    source: str,
    taric10: str | None,
    cn8: str | None,
    table_reads: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    metrics: dict[str, Any],
    source_product_facts_id: str = "",
    source_routing_context_id: str = "",
    document_package_id: str = "",
    candidate_id: str = "",
    missing_facts: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Logical output for baseline document shell retrieval.

    Producer:
      DocumentAgent logical baseline worker.

    Runtime source:
      baseline_document_master + document_binding.

    Purpose:
      Give UI/validator/admin one stable baseline-document object without
      forcing them to understand raw DetailedRequirement rows.
    """
    return {
        "object_type": "DocBaseSet",
        "dto_name": "DocBaseSet_dto",
        "source": source,
        "document_package_id": document_package_id,
        "candidate_id": candidate_id,
        "source_product_facts_id": source_product_facts_id,
        "source_routing_context_id": source_routing_context_id,
        "taric10": taric10 or "",
        "cn8": cn8 or "",
        "table_reads": table_reads,
        "requirements": requirements,
        "documents": documents,
        "missing_facts": missing_facts or [],
        "metrics": metrics,
        **extra,
    }


def DocPreSet_dto(
    *,
    source: str,
    taric10: str | None,
    cn8: str | None,
    table_reads: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    metrics: dict[str, Any],
    source_product_facts_id: str = "",
    source_routing_context_id: str = "",
    document_package_id: str = "",
    candidate_id: str = "",
    missing_facts: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Logical output for chapter/domain/pre-gate checks.

    Producer:
      DocumentAgent logical pre-TARIC worker.

    Runtime source:
      pre_taric_requirement_master.

    Purpose:
      Preserve requirements that are knowable before final post-TARIC
      certificate/detail matching, e.g. SPS/organic/CITES/sanction gates.
    """
    return {
        "object_type": "DocPreSet",
        "dto_name": "DocPreSet_dto",
        "source": source,
        "document_package_id": document_package_id,
        "candidate_id": candidate_id,
        "source_product_facts_id": source_product_facts_id,
        "source_routing_context_id": source_routing_context_id,
        "taric10": taric10 or "",
        "cn8": cn8 or "",
        "table_reads": table_reads,
        "checks": checks,
        "missing_facts": missing_facts or [],
        "metrics": metrics,
        **extra,
    }


def DocPostSet_dto(
    *,
    source: str,
    taric10: str | None,
    cn8: str | None,
    table_reads: list[dict[str, Any]],
    status: str,
    message: str,
    requirements: list[dict[str, Any]],
    metrics: dict[str, Any],
    source_codeset_id: str = "",
    document_package_id: str = "",
    candidate_id: str = "",
    missing_facts: list[str] | None = None,
    celex_basis: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Logical output for TARIC-triggered document requirements.

    Producer:
      DocumentAgent logical post-TARIC worker.

    Runtime source:
      post_taric_requirement_master
      + taric_certificate_declaration_guidance
      + taric_celex_table.

    Purpose:
      Record measure/certificate/legal-base-triggered requirements separately
      from baseline and pre-TARIC screening.
    """
    return {
        "object_type": "DocPostSet",
        "dto_name": "DocPostSet_dto",
        "source": source,
        "document_package_id": document_package_id,
        "candidate_id": candidate_id,
        "source_codeset_id": source_codeset_id,
        "taric10": taric10 or "",
        "cn8": cn8 or "",
        "table_reads": table_reads,
        "status": status,
        "message": message,
        "requirements": requirements,
        "missing_facts": missing_facts or [],
        "celex_basis": celex_basis or [],
        "metrics": metrics,
        **extra,
    }


def DocBindSet_dto(**values: Any) -> dict[str, Any]:
    return {"object_type": "DocBindSet", "dto_name": "DocBindSet_dto", **values}


def DocView(**values: Any) -> dict[str, Any]:
    return {"object_type": "DocView", "dto_name": "DocView", **values}


def Decision(**values: Any) -> dict[str, Any]:
    return {"object_type": "OrchestratorDecision", "dto_name": "Decision", **values}

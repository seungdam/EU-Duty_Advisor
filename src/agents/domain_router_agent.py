"""
Domain_Router_Agent — blackboard wrapper around DomainRouterTool.

The tool stays deterministic and table-driven. This agent exists to make the
ProductUnderstanding -> DomainRouter -> Classification handoff auditable in
the Blackboard.
"""
from __future__ import annotations

from agents import dto
from agents.agent_base import BaseAgent
from agents.blackboard import BlackboardStore, now_iso
from agents.tools import DomainRouterTool


class DomainRouterAgent(BaseAgent):
    agent_name = "Domain_Router_Agent"
    stage = "Regulatory_Domain_Routing"
    llm_model = None

    def __init__(self) -> None:
        super().__init__()
        self._tool = DomainRouterTool()

    @staticmethod
    def _classifier_lane(*, understanding: dict, routed: dict) -> dict:
        chapters = [
            str(c.get("chapter") or "").zfill(2)
            for c in (routed.get("candidate_chapters") or [])
            if c.get("chapter")
        ]
        blocked = [
            str(c.get("chapter") or "").zfill(2)
            for c in (routed.get("blocked_chapters") or [])
            if c.get("chapter")
        ]
        query_terms = []
        for key in (
            "chapter_routing_terms",
            "principal_ingredient_terms",
            "ingredient_taxonomy_terms",
            "processing_terms",
            "product_form_terms",
            "routing_keywords",
        ):
            for term in understanding.get(key) or []:
                text = str(term or "").strip()
                if text and text not in query_terms:
                    query_terms.append(text)
        identity_terms = [
            str(understanding.get("commercial_identity") or "").strip(),
            str(understanding.get("translated_product_name") or "").strip(),
            str(understanding.get("normalized_tariff_description") or "").strip(),
        ]
        identity_terms = [x for x in identity_terms if x]
        primary_parts = list(query_terms[:8]) or list(identity_terms[:2])
        for text in identity_terms:
            # Structured routing terms should lead. Full LLM descriptions are
            # useful context, but should not dominate the classifier query when
            # they contain a stray phrase such as "rice dish".
            if text and text not in primary_parts and len(primary_parts) < 10:
                primary_parts.append(text)
        primary_query = "; ".join(primary_parts)
        negative_terms = []
        for item in (
            (understanding.get("blocked_routing_terms") or [])
            + (understanding.get("excluded_from_routing_terms") or [])
        ):
            if isinstance(item, dict):
                term = str(item.get("term") or "").strip()
                if term and term not in negative_terms:
                    negative_terms.append(term)
        return {
            "allowed_chapters": chapters[:3],
            "soft_allowed_chapters": chapters[3:],
            "blocked_chapters": blocked,
            "primary_query": primary_query,
            "query_terms": query_terms[:80],
            "boost_terms": query_terms[:20],
            "negative_terms": negative_terms[:40],
            "exclude_if_matched": (
                (understanding.get("blocked_routing_terms") or [])
                + (understanding.get("excluded_from_routing_terms") or [])
            ),
        }

    @staticmethod
    def _document_lane(*, routed: dict) -> dict:
        chapters = [
            str(c.get("chapter") or "").zfill(2)
            for c in (routed.get("candidate_chapters") or [])
            if c.get("chapter")
        ]
        primary_chapters = chapters[:1]
        candidate_chapters = chapters[:5]
        return {
            "baseline_policy": "include_core_baseline_documents",
            "baseline_groups": [
                "trade_baseline",
                "product_baseline",
                "origin_baseline",
            ],
            "pre_taric_scope": {
                "chapters": primary_chapters,
                "candidate_chapters": candidate_chapters,
                "domains": list(routed.get("domain_scopes") or []),
                "pre_gates": list(routed.get("pre_gate_domains") or []),
                "confidence_policy": "primary_required_retained_conditional",
            },
        }

    def run(self, store: BlackboardStore) -> None:
        bb = store.load()
        pes = bb.get("product_evidence_state") or {}
        understanding = bb.get("product_understanding") or {}
        if not pes:
            raise RuntimeError("No ProductEvidenceState on the Blackboard.")
        if not understanding:
            raise RuntimeError("No ProductUnderstandingFacts on the Blackboard.")

        product_id = pes.get("product_id") or ""
        understanding_id = understanding.get("understanding_id") or ""
        self.read_input(product_id)
        self.read_input(understanding_id)

        observed_facts = pes.get("observed_facts") or {}
        routed = self._tool.route_product(
            product_understanding=understanding,
            product_facts=observed_facts,
            top_k=5,
        )

        for chapter in routed.get("candidate_chapters") or []:
            self.cite(
                "cn_chapter_index",
                str(chapter.get("chapter") or ""),
                snippet=str(chapter.get("matched_terms") or [])[:160],
                reason="DomainRouterTool pre-classification chapter candidate.",
            )
        for ev in routed.get("evidence") or []:
            if ev.get("source") == "domain_scope_routes":
                self.cite(
                    "domain_scope_routes",
                    str(ev.get("domain_scope") or ev.get("chapter") or ""),
                    snippet=str(ev)[:160],
                    reason="Domain scope candidate for downstream baseline/pre-TARIC lookup.",
                )

        routing_id = store.next_id("route")
        out = dto.Route_dto(
            created_by=self.agent_name,
            created_at=now_iso(),
            routing_context_id=routing_id,
            product_id=product_id,
            source_product_facts_id=understanding_id,
            candidate_chapters=list(routed.get("candidate_chapters") or []),
            blocked_chapters=list(routed.get("blocked_chapters") or []),
            domain_scopes=list(routed.get("domain_scopes") or []),
            pre_gate_domains=list(routed.get("pre_gate_domains") or []),
            processing_state=str(routed.get("processing_state") or ""),
            soft_filter=bool(routed.get("soft_filter", True)),
            routing_basis=dict(routed.get("routing_basis") or {}),
            missing_facts=list(routed.get("missing_facts") or []),
            evidence=list(routed.get("evidence") or []),
            classifier_lane=self._classifier_lane(
                understanding=understanding,
                routed=routed,
            ),
            document_lane=self._document_lane(routed=routed),
        )
        store.put("routing_context", out)
        self.wrote(routing_id)
        top = (out.get("candidate_chapters") or [{}])[0]
        self.reason(
            f"Built RoutingContext {routing_id}: top_chapter={top.get('chapter') or '-'} "
            f"domain_scopes={out.get('domain_scopes') or []} "
            f"pre_gate_domains={out.get('pre_gate_domains') or []}."
        )

"""
Domain_Router_Agent — blackboard wrapper around DomainRouterTool.

The tool stays deterministic and table-driven. This agent exists to make the
ProductUnderstanding -> DomainRouter -> Classification handoff auditable in
the Blackboard.
"""
from __future__ import annotations

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
        out = {
            "object_type": "RoutingContext",
            "created_by": self.agent_name,
            "created_at": now_iso(),
            "routing_context_id": routing_id,
            "product_id": product_id,
            "source_understanding_id": understanding_id,
            **routed,
        }
        store.put("routing_context", out)
        self.wrote(routing_id)
        top = (out.get("candidate_chapters") or [{}])[0]
        self.reason(
            f"Built RoutingContext {routing_id}: top_chapter={top.get('chapter') or '-'} "
            f"domain_scopes={out.get('domain_scopes') or []} "
            f"pre_gate_domains={out.get('pre_gate_domains') or []}."
        )

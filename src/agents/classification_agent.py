"""
Classification_Agent — delegates to ASAPExpress Stage 1 classifier.

Inside BaseAgent.execute() this agent:
  1. Reads ProductEvidenceState from the Blackboard.
  2. Hands it to agents._external_classifier.run_external_classifier(),
     which runs the full ASAPExpress 7-step Stage 1 pipeline
     (retriever → context → evidence → request → LLM → validator →
     decision → traversal → recommendation).
  3. Translates the Stage1RecommendationReport back into CandidateCode
     entries and stamps citations + reasoning trace.

ASAPExpress code is loaded as-is via sys.path — no modifications.
"""
from __future__ import annotations

import os
from dataclasses import asdict, is_dataclass

from agents._external_classifier import (
    ExternalClassificationResult,
    buckets_for_chapters,
    classifier_fallback_scopes,
    normalize_router_scope,
    run_external_classifier,
)
from agents.agent_base import BaseAgent
from agents.tools import TaricBranchResolverTool
from agents.blackboard import BlackboardStore, now_iso


# Classifier candidate buckets follow the WCO 9-group scheme defined in
# agents._external_classifier (single source of truth). DomainRouter candidate
# chapters are mapped to bucket keys via buckets_for_chapters() at the
# router/classifier boundary in _with_routing_context below.


def _read_field(obj, *names, default=None):
    """Read a field from a dict, dataclass, or object — tries each name."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        for n in names:
            if n in obj and obj[n] is not None:
                return obj[n]
        return default
    if is_dataclass(obj):
        obj = asdict(obj)
        for n in names:
            if n in obj and obj[n] is not None:
                return obj[n]
        return default
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


class ClassificationAgent(BaseAgent):
    agent_name = "Classification_Agent"
    stage = "Classification"
    llm_model = os.environ.get("EU_EXPORT_LLM_MODEL", "gemma4-ctx")

    def __init__(self) -> None:
        super().__init__()
        self._taric_resolver = TaricBranchResolverTool()

    def run(self, store: BlackboardStore) -> None:
        bb = store.load()
        pes = bb.get("product_evidence_state") or {}
        if not pes:
            raise RuntimeError("No ProductEvidenceState on the Blackboard.")
        self.read_input(pes["product_id"])
        product_understanding = bb.get("product_understanding") or {}
        routing_context = bb.get("routing_context") or {}
        if product_understanding.get("understanding_id"):
            self.read_input(product_understanding["understanding_id"])
        if routing_context.get("routing_context_id"):
            self.read_input(routing_context["routing_context_id"])

        # Step 0 — handle pending challenges first. If another agent has
        # raised an open challenge against one of our candidates, write a
        # ChallengeResponse instead of running ASAPExpress Stage 1 again.
        open_challenges = self._read_open_challenges_for_me(bb)
        if open_challenges:
            self._respond_to_challenges(store, open_challenges)
            return

        classifier_pes = self._with_routing_context(
            pes,
            product_understanding=product_understanding,
            routing_context=routing_context,
        )
        result: ExternalClassificationResult = run_external_classifier(classifier_pes)

        # Cite candidates from the retriever (every shortlisted CN8).
        for c in result.citations:
            self.cite(
                c["source_table"], c["source_id"],
                snippet=c.get("snippet", ""),
                reason=c.get("reason", ""),
            )

        if result.prompt_text:
            self.record_prompt(result.prompt_text)
        if result.llm_model:
            self.llm_model = result.llm_model

        if result.error:
            self.reason(f"ASAPExpress classifier returned error: {result.error}")
            if not self._emit_retriever_fallback(store, pes, why=result.error):
                self._emit_unresolved(store, pes, why=result.error)
            return

        # Preserve the LLM response excerpt in reasoning_summary so the admin
        # viewer can debug Stage1ResponseValidator rejections.
        resp_snippet = (result.llm_response_text or "").strip()
        if resp_snippet:
            self.reason(f"LLM response[:300]: {resp_snippet[:300]!r}")

        recommendation = result.recommendation
        if recommendation is None:
            self.reason("No Stage1RecommendationReport produced; emitting needs_more_facts.")
            if not self._emit_retriever_fallback(store, pes, why="no_recommendation"):
                self._emit_unresolved(store, pes, why="no_recommendation")
            return

        # Extract candidate dicts from the Stage1 recommendation
        recommended = _read_field(recommendation, "recommendedCandidate") or {}
        retained = _read_field(recommendation, "retainedCandidates") or []

        emitted: list[dict] = []
        if recommended:
            emitted.append({
                "hs8": _read_field(recommended, "hs8", default="") or "",
                "reason": _read_field(recommended, "reason", "rationale", default="") or "Recommended by ASAPExpress Stage 1.",
                "rank": 1,
                "confidence": 0.7,
                "status": "proposed",
            })
        for i, r in enumerate(retained, start=2):
            emitted.append({
                "hs8": _read_field(r, "hs8", default="") or "",
                "reason": _read_field(r, "reason", "rationale", default="") or "Retained candidate.",
                "rank": i,
                "confidence": 0.5,
                "status": "proposed",
            })

        decision_status = _read_field(result.decision_report, "decisionStatus", default="unknown")
        traversal_status = _read_field(result.traversal_report, "traversalStatus", default="unknown")
        self.reason(
            f"Stage 1 decision={decision_status} traversal={traversal_status}; "
            f"emitted {len(emitted)} candidate(s)."
        )

        if not emitted:
            self.reason("ASAPExpress returned no recommended/retained candidates.")
            why = f"no_recommendation_or_retained ({decision_status})"
            if not self._emit_retriever_fallback(store, pes, why=why):
                self._emit_unresolved(store, pes, why=why)
            return

        ccs_id = store.next_id("ccs")
        ccs_candidates: list[dict] = []
        for c in emitted:
            cn8 = (c["hs8"] or "")[:8]
            if not cn8.isdigit() or len(cn8) != 8:
                self.reason(f"Skipped invalid emitted CN8 candidate: {c.get('hs8')!r}.")
                continue
            taric_branches = self._resolve_taric_branches(cn8)
            selected_branch = self._select_taric_branch(taric_branches)
            taric10 = selected_branch.get("taric10") or ""
            if not taric10:
                self.reason(
                    f"No TARIC branch found for CN8={cn8}; taric10 left blank "
                    "instead of synthesizing cn8 + '00'."
                )
            cand_id = store.next_id("cand")
            ccs_candidates.append({
                "candidate_id": cand_id,
                "hs6": cn8[:6],
                "cn8": cn8,
                "taric10": taric10,
                "taric10_branch_candidates": taric_branches,
                "taric10_resolution_mode": (
                    "enumerate_all_under_cn8" if taric_branches else "no_taric_branch_found"
                ),
                "taric10_is_recommended": False,
                "taric10_branch_count": len(taric_branches),
                "selected_taric10_reason": (
                    selected_branch.get("selection_reason")
                    if taric10 else "No TARIC10 branch resolved from current master table."
                ),
                "primary_taric10_reason": (
                    selected_branch.get("selection_reason")
                    if taric10 else "No TARIC10 branch resolved from current master table."
                ),
                "rank": c["rank"],
                "confidence": c["confidence"],
                "status": c["status"],
                "candidate_source": "classifier",
                "classification_basis": [str(c["reason"])[:300]],
                "classification_citations": list(self._ontology_reads),
                "required_facts": [],
                "unknowns": [],
            })
        if not ccs_candidates:
            self.reason("No valid emitted CN8 candidates remained after validation.")
            if not self._emit_retriever_fallback(store, pes, why="no_valid_emitted_cn8"):
                self._emit_unresolved(store, pes, why="no_valid_emitted_cn8")
            return
        store.append("candidate_code_sets", {
            "object_type": "CandidateCodeSet",
            "created_by": self.agent_name,
            "created_at": now_iso(),
            "candidate_set_id": ccs_id,
            "product_id": pes["product_id"],
            "candidates": ccs_candidates,
        })
        self.wrote(ccs_id)
        for c in ccs_candidates:
            self.wrote(c["candidate_id"])

    def _with_routing_context(
        self,
        pes: dict,
        *,
        product_understanding: dict,
        routing_context: dict,
    ) -> dict:
        """Add ProductUnderstanding/DomainRouter hints without hard filtering."""
        out = dict(pes)
        obs = dict(pes.get("observed_facts") or {})
        hint_lines: list[str] = []

        if product_understanding:
            obs["product_understanding"] = product_understanding
            processing_state = product_understanding.get("processing_state")
            if processing_state:
                hint_lines.append(f"ProductUnderstanding processing_state: {processing_state}")
            keywords = product_understanding.get("routing_terms") or product_understanding.get("keywords") or []
            if keywords:
                hint_lines.append("ProductUnderstanding routing terms: " + ", ".join(str(k) for k in keywords[:30]))

        if routing_context:
            obs["routing_context"] = routing_context
            candidate_chapters = [
                str(c.get("chapter")).zfill(2)
                for c in (routing_context.get("candidate_chapters") or [])
                if c.get("chapter")
            ]
            obs["candidate_chapters"] = candidate_chapters

            # Deterministic chapter -> WCO group / demo sub-bucket. This replaces
            # keyword-based scope routing, so every chapter resolves to a real
            # bucket (full 01-97 coverage) instead of mis-routing into food /
            # cosmetics. Falls back to the legacy regulatory-scope map, then to
            # the demo buckets, when no candidate chapter is available.
            bucket_scopes = buckets_for_chapters(candidate_chapters)
            scope_source = "candidate_chapters"
            if not bucket_scopes:
                for raw in routing_context.get("domain_scopes") or []:
                    bucket = normalize_router_scope(raw)
                    if bucket and bucket not in bucket_scopes:
                        bucket_scopes.append(bucket)
                scope_source = "regulatory_domain_scopes"
            if not bucket_scopes:
                bucket_scopes = classifier_fallback_scopes()
                scope_source = "demo_fallback"
            obs["domain_scopes"] = bucket_scopes
            self.reason(
                f"Classifier buckets {bucket_scopes} via {scope_source} "
                f"(router chapters={candidate_chapters[:5]})."
            )

            for c in (routing_context.get("candidate_chapters") or [])[:5]:
                hint_lines.append(
                    "DomainRouter chapter candidate: "
                    f"{c.get('chapter')} {c.get('chapter_title') or ''}; "
                    f"confidence={c.get('confidence')}; "
                    f"matched_terms={', '.join(str(t) for t in (c.get('matched_terms') or [])[:12])}"
                )
            blocked = routing_context.get("blocked_chapters") or []
            if blocked:
                hint_lines.append(
                    "DomainRouter guardrail blocked raw chapters: "
                    + "; ".join(
                        f"{b.get('chapter')} -> {','.join(b.get('redirect_chapters') or [])}"
                        for b in blocked[:5]
                    )
                )

        original = obs.get("classification_input_fact_texts") or []
        if not isinstance(original, list):
            original = [str(original)]
        obs["classification_input_fact_texts"] = [*hint_lines, *original]
        out["observed_facts"] = obs
        if hint_lines:
            self.reason(f"Added {len(hint_lines)} ProductUnderstanding/DomainRouter hint line(s) to classifier input.")
        return out

    def _resolve_taric_branches(self, cn8: str) -> list[dict]:
        if not cn8 or cn8 == "99999999":
            return []
        try:
            branches = self._taric_resolver.resolve(
                cn8,
                only_declarable_leaf=False,
                only_kr_applicable=False,
            )
        except Exception as exc:  # noqa: BLE001
            self.reason(f"TaricBranchResolverTool error for CN8={cn8}: {exc}")
            return []

        out = [b.to_dict() for b in branches]
        if out:
            self.cite(
                "taric_master_table",
                f"cn8={cn8}",
                snippet=f"{len(out)} TARIC10 branch candidate(s)",
                reason="TaricBranchResolverTool branch retrieval.",
            )
        return out

    def _emit_retriever_fallback(
        self,
        store: BlackboardStore,
        pes: dict,
        *,
        why: str,
        limit: int = 10,
    ) -> bool:
        """Keep Blackboard moving when the LLM review fails.

        The retriever shortlist is not a final classification. It is still a
        useful candidate set for downstream TARIC/document smoke tests, so we
        publish it with low confidence and an explicit retriever_fallback tag.
        """
        shortlist: list[dict] = []
        seen: set[str] = set()
        for citation in self._ontology_reads:
            if citation.get("source_table") not in {"cn_table", "cn_hs8_pair_rows"}:
                continue
            cn8 = str(citation.get("source_id") or "")[:8]
            if not cn8.isdigit() or len(cn8) != 8 or cn8 in seen:
                continue
            seen.add(cn8)
            shortlist.append(dict(citation))
            if len(shortlist) >= limit:
                break

        if not shortlist:
            self.reason("No retriever shortlist available; trying LLM classifier fallback.")
            return self._emit_llm_classifier_fallback(store, pes, why=why)

        ccs_id = store.next_id("ccs")
        candidates: list[dict] = []
        for rank, citation in enumerate(shortlist, start=1):
            cn8 = str(citation.get("source_id") or "")[:8]
            taric_branches = self._resolve_taric_branches(cn8)
            selected_branch = self._select_taric_branch(taric_branches)
            taric10 = selected_branch.get("taric10") or ""
            if not taric10:
                self.reason(f"Retriever fallback CN8={cn8} has no TARIC10 branch.")
            cand_id = store.next_id("cand")
            candidates.append({
                "candidate_id": cand_id,
                "hs6": cn8[:6],
                "cn8": cn8,
                "taric10": taric10,
                "taric10_branch_candidates": taric_branches,
                "taric10_resolution_mode": (
                    "enumerate_all_under_cn8" if taric_branches else "no_taric_branch_found"
                ),
                "taric10_is_recommended": False,
                "taric10_branch_count": len(taric_branches),
                "selected_taric10_reason": (
                    selected_branch.get("selection_reason")
                    if taric10 else "No TARIC10 branch resolved from current master table."
                ),
                "primary_taric10_reason": (
                    selected_branch.get("selection_reason")
                    if taric10 else "No TARIC10 branch resolved from current master table."
                ),
                "rank": rank,
                "confidence": max(0.15, round(0.45 - ((rank - 1) * 0.03), 2)),
                "status": "proposed",
                "candidate_source": "retriever_fallback",
                "classification_basis": [
                    f"Retriever shortlist fallback because LLM classification failed: {why}",
                    f"Retriever evidence: {citation.get('snippet') or cn8}",
                ],
                "classification_citations": [citation],
                "required_facts": ["llm_classification_retry"],
                "unknowns": [why],
            })

        store.append("candidate_code_sets", {
            "object_type": "CandidateCodeSet",
            "created_by": self.agent_name,
            "created_at": now_iso(),
            "candidate_set_id": ccs_id,
            "product_id": pes["product_id"],
            "classification_status": "retriever_fallback",
            "failure_reason": why,
            "shortlisted_candidates": shortlist,
            "candidates": candidates,
        })
        self.wrote(ccs_id)
        for candidate in candidates:
            self.wrote(candidate["candidate_id"])
        self.reason(
            f"LLM classification unresolved ({why}); wrote {len(candidates)} "
            "retriever fallback candidate(s) for downstream TARIC/document checks."
        )
        return True

    # ----------------------------------------------------- llm_classifier fallback
    def _emit_llm_classifier_fallback(
        self,
        store: BlackboardStore,
        pes: dict,
        *,
        why: str,
        top_k: int = 5,
    ) -> bool:
        """LLM-classifier-based fallback when ASAPExpress retriever returns nothing.

        Used when Korean-only product evidence prevents the keyword retriever
        from shortlisting any cn_table row. Falls back to llm_classifier which
        runs embedding + LLM over the full cn_table to propose candidates.
        """
        from agents.llm_classifier import classify as llm_classify

        obs = pes.get("observed_facts") or {}
        parts: list[str] = []
        if obs.get("product_name"):
            parts.append(f"product_name: {obs['product_name']}")
        if obs.get("page_title"):
            parts.append(f"page_title: {obs['page_title']}")
        if obs.get("description"):
            parts.append(f"description: {obs['description']}")
        comp = obs.get("composition")
        if isinstance(comp, list) and comp:
            joined = "\n".join(str(c) for c in comp if c)[:2000]
            if joined:
                parts.append(f"composition:\n{joined}")
        product_input = "\n".join(parts).strip()
        if not product_input:
            self.reason("LLM fallback skipped: empty product_input.")
            return False

        try:
            results = llm_classify(product_input, top_k=top_k)
        except Exception as exc:  # pragma: no cover — fallback never raises upward
            self.reason(f"LLM classifier exception: {exc}")
            return False

        if not results:
            self.reason("LLM classifier produced no candidates.")
            return False

        ccs_id = store.next_id("ccs")
        candidates: list[dict] = []
        for rank, r in enumerate(results, start=1):
            cn8 = str(r.get("결정세번") or r.get("cn8") or "").strip()
            cn8 = "".join(ch for ch in cn8 if ch.isdigit())[:8]
            if len(cn8) != 8:
                continue
            taric_branches = self._resolve_taric_branches(cn8)
            selected_branch = self._select_taric_branch(taric_branches)
            taric10 = selected_branch.get("taric10") or ""
            cand_id = store.next_id("cand")
            try:
                confidence = float(r.get("신뢰도") or r.get("confidence") or 0.3)
            except (TypeError, ValueError):
                confidence = 0.3
            reason_txt = str(r.get("분류사유_영문") or r.get("reason") or "")[:300]
            candidates.append({
                "candidate_id": cand_id,
                "hs6": cn8[:6],
                "cn8": cn8,
                "taric10": taric10,
                "taric10_branch_candidates": taric_branches,
                "taric10_resolution_mode": (
                    "enumerate_all_under_cn8" if taric_branches else "no_taric_branch_found"
                ),
                "taric10_is_recommended": False,
                "taric10_branch_count": len(taric_branches),
                "selected_taric10_reason": (
                    selected_branch.get("selection_reason")
                    if taric10 else "No TARIC10 branch resolved from current master table."
                ),
                "primary_taric10_reason": (
                    selected_branch.get("selection_reason")
                    if taric10 else "No TARIC10 branch resolved from current master table."
                ),
                "rank": rank,
                "confidence": round(max(0.0, min(1.0, confidence)), 3),
                "status": "proposed",
                "candidate_source": "llm_classifier_fallback",
                "classification_basis": [
                    f"LLM classifier fallback because retriever failed: {why}",
                    reason_txt or f"LLM ranked {rank} for cn8 {cn8}.",
                ],
                "classification_citations": [],
                "required_facts": [],
                "unknowns": [why],
            })

        if not candidates:
            self.reason("LLM classifier returned rows but none parsed to valid CN8.")
            return False

        store.append("candidate_code_sets", {
            "object_type": "CandidateCodeSet",
            "created_by": self.agent_name,
            "created_at": now_iso(),
            "candidate_set_id": ccs_id,
            "product_id": pes["product_id"],
            "candidates": candidates,
        })
        self.wrote(ccs_id)
        for c in candidates:
            self.wrote(c["candidate_id"])
        self.reason(
            f"LLM classifier fallback produced {len(candidates)} candidate(s) "
            f"(top1 cn8={candidates[0]['cn8']})."
        )
        return True

    # ------------------------------------------------------------------ challenges
    def _collect_my_candidate_ids(self, bb: dict) -> set[str]:
        """All candidate IDs from CCS authored by this agent."""
        out: set[str] = set()
        for ccs in bb.get("candidate_code_sets") or []:
            if ccs.get("created_by") != self.agent_name:
                continue
            for c in ccs.get("candidates") or []:
                cid = c.get("candidate_id")
                if cid:
                    out.add(cid)
        return out

    def _select_taric_branch(self, branches: list[dict]) -> dict:
        """Pick a compatibility primary TARIC10 from deterministic branches.

        This is not a legal recommendation. The full branch list remains on
        the candidate and Document_Agent packages every branch. The primary
        value only preserves older UI/API paths that expect cand["taric10"].
        """
        if not branches:
            return {}

        def score(branch: dict) -> tuple:
            description = (branch.get("branch_description") or "").strip().lower()
            return (
                1 if branch.get("applies_to_origin_kr") else 0,
                1 if branch.get("is_declarable_leaf") else 0,
                0 if branch.get("needs_review") else 1,
                int(branch.get("measure_row_count") or 0),
                1 if description == "other" else 0,
            )

        selected = max(branches, key=score)
        out = dict(selected)
        out["selection_reason"] = (
            "Compatibility primary only, not a TARIC10 recommendation. "
            "All TARIC master branches under this CN8 are retained in "
            "taric10_branch_candidates; this primary prefers KR-applicable "
            "declarable leaves, then non-review branches, then measure coverage."
        )
        return out

    def _read_open_challenges_for_me(self, bb: dict) -> list[dict]:
        """Open challenges that target one of our candidates and were raised
        by someone else. We skip our own challenges and resolved ones.
        """
        my_cands = self._collect_my_candidate_ids(bb)
        if not my_cands:
            return []
        out: list[dict] = []
        for chg in bb.get("challenges") or []:
            if chg.get("status") != "open":
                continue
            if chg.get("raised_by") == self.agent_name:
                continue
            target = chg.get("target_candidate_id")
            if target and target in my_cands:
                out.append(chg)
        return out

    def _respond_to_challenges(
        self,
        store: BlackboardStore,
        challenges: list[dict],
    ) -> None:
        """For each open challenge against our candidate, write a single
        ChallengeResponse AND close the source challenge (status=resolved).

        Decision rule (stub):
          - challenge_type == measure_document_mismatch  → needs_more_facts
            (Classification cannot invent product facts on its own.)
          - any other type                                → needs_more_facts
            (until a richer decision policy is wired in.)
        """
        target_chg_ids = {chg["challenge_id"] for chg in challenges}
        bb = store.load()
        for chg in challenges:
            self.read_input(chg["challenge_id"])
            chg_type = chg.get("challenge_type") or "unknown"
            target_cand = chg.get("target_candidate_id") or "?"
            if chg_type == "measure_document_mismatch":
                reason_text = (
                    f"Candidate {target_cand} returned no TARIC measure rows. "
                    "Classification cannot reclassify without additional product "
                    "facts (composition pct, intended use, processing state, "
                    "establishment approval). Routing to the user."
                )
            else:
                reason_text = (
                    f"Acknowledged challenge {chg['challenge_id']} of type "
                    f"{chg_type}; no automatic action available — escalating."
                )

            resp_id = store.next_id("rsp")
            store.append("challenge_responses", {
                "object_type": "ChallengeResponse",
                "created_by": self.agent_name,
                "created_at": now_iso(),
                "response_id": resp_id,
                "responds_to": chg["challenge_id"],
                "action": "needs_more_facts",
                "reason": reason_text,
                "updates": [],
                "status": "resolved",
            })
            self.wrote(resp_id)
            self.reason(
                f"Wrote ChallengeResponse {resp_id} to {chg['challenge_id']} "
                f"({chg_type}, raised_by={chg.get('raised_by')}) → "
                "action=needs_more_facts."
            )
        # Close the source Challenges so Orchestrator does not count them
        # as still-open. store.append above re-loaded internally, so we
        # do a final load+save here to mutate challenges in place.
        bb = store.load()
        for c in bb.get("challenges") or []:
            if c.get("challenge_id") in target_chg_ids and c.get("status") == "open":
                c["status"] = "resolved"
        store.save(bb)

    # ------------------------------------------------------------------ helpers
    def _emit_unresolved(self, store: BlackboardStore, pes: dict, *, why: str) -> None:
        ccs_id = store.next_id("ccs")
        store.append("candidate_code_sets", {
            "object_type": "CandidateCodeSet",
            "created_by": self.agent_name,
            "created_at": now_iso(),
            "candidate_set_id": ccs_id,
            "product_id": pes["product_id"],
            "classification_status": "needs_more_facts",
            "failure_reason": why,
            "shortlisted_candidates": list(self._ontology_reads),
            "candidates": [],
        })
        self.wrote(ccs_id)
        self.reason(
            f"Classification unresolved ({why}); wrote empty CandidateCodeSet "
            "instead of a synthetic 99999999 candidate."
        )

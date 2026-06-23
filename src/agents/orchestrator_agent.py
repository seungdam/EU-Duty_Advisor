"""
Orchestrator_Agent — stub.

Reads the entire Blackboard, decides the user-facing decision_status, and
writes a single OrchestratorDecision. Per Agent_Behavior_Guardrails, the
Orchestrator:

  - must not silently overwrite other Agents' outputs
  - must not expose internal Agent debate to the user
  - may surface unresolved challenges as needs_review / ask_user
"""
from __future__ import annotations

from agents.agent_base import BaseAgent
from agents.blackboard import BlackboardStore, now_iso


class OrchestratorAgent(BaseAgent):
    agent_name = "Orchestrator_Agent"
    stage = "Orchestration"
    llm_model = None  # decision is deterministic; user-facing summary stays LLM-free for now

    def run(self, store: BlackboardStore) -> None:
        bb = store.load()
        pes = bb.get("product_evidence_state") or {}
        ccs_list = bb.get("candidate_code_sets") or []
        document_packages = bb.get("document_packages") or []
        challenges = bb.get("challenges") or []
        open_challenges = [c for c in challenges if c.get("status") == "open"]
        backtracking_signals = [
            signal
            for dp in document_packages
            for signal in (dp.get("backtracking_signals") or [])
        ]

        if not pes:
            raise RuntimeError("OrchestratorAgent cannot run without a ProductEvidenceState.")
        self.read_input(pes["product_id"])

        if not ccs_list:
            decision_status = "needs_review"
            selected: list[str] = []
            self.reason("No CandidateCodeSet produced; decision_status=needs_review.")
        else:
            latest = ccs_list[-1]
            self.read_input(latest["candidate_set_id"])
            cands = latest["candidates"]
            if open_challenges:
                decision_status = "needs_review"
                selected = []
                self.reason(
                    f"{len(open_challenges)} open challenges present; decision_status=needs_review."
                )
            elif backtracking_signals:
                decision_status = "needs_review"
                selected = []
                self.reason(
                    f"{len(backtracking_signals)} document backtracking signal(s) present; "
                    "decision_status=needs_review."
                )
            elif len(cands) == 1 and cands[0]["status"] == "proposed":
                decision_status = "accepted"
                selected = [cands[0]["candidate_id"]]
                self.reason(f"Single accepted candidate {cands[0]['candidate_id']}.")
            elif len(cands) > 1:
                decision_status = "multiple_candidates"
                selected = [c["candidate_id"] for c in cands]
                self.reason(f"{len(cands)} candidates remain; surfacing as multiple_candidates.")
            elif not cands and latest.get("classification_status"):
                decision_status = "needs_review"
                selected = []
                self.reason(
                    "Classification produced no candidate. "
                    f"status={latest.get('classification_status')} "
                    f"reason={latest.get('failure_reason') or 'unknown'}; "
                    "decision_status=needs_review."
                )
            else:
                decision_status = "ask_user"
                selected = []
                self.reason("Candidate status requires more facts; routing to ask_user.")

        dec_id = store.next_id("dec")
        visible_warnings = [
            f"{c.get('severity', 'unknown')} challenge: {c.get('issue', '')}"
            for c in open_challenges
        ] + [
            f"document backtracking: {s.get('type', 'unknown')} - {s.get('reason', '')}"
            for s in backtracking_signals
        ]
        if ccs_list:
            latest_ccs = ccs_list[-1]
            if latest_ccs.get("classification_status") and not latest_ccs.get("candidates"):
                visible_warnings.append(
                    "classification unresolved: "
                    f"{latest_ccs.get('failure_reason') or latest_ccs.get('classification_status')}"
                )
        decision = {
            "object_type": "OrchestratorDecision",
            "created_by": self.agent_name,
            "created_at": now_iso(),
            "decision_id": dec_id,
            "product_id": pes["product_id"],
            "decision_status": decision_status,
            "selected_candidate_ids": selected,
            "document_package_ids": [dp.get("document_package_id") for dp in document_packages if dp.get("document_package_id")],
            "visible_warnings": visible_warnings,
            "user_questions": [],
            "display_plan": {
                "default_view": "candidate_list" if selected else "ask_user",
                "candidate_click_action": "show_document_package",
                "show_agent_timeline": False,   # user-facing: hidden by default
                "show_backtracking_events": False,
            },
            "reason": " ".join(self._reasoning_chunks) or "stub orchestration",
        }
        store.append("orchestrator_decisions", decision)
        self.wrote(dec_id)

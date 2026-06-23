"""
BaseAgent — strict interface every ASAP v2 Agent must follow.

Goal: enforce the audit-trail contract from
``runtime_contract/Agent_Behavior_Guardrails.md``. Every concrete Agent must
emit, for each invocation:

  - inputs_read / outputs_written  → which Blackboard object IDs it touched
  - ontology_reads                  → which core rows (EvidenceCitation)
                                      it consulted
  - reasoning_summary               → one-paragraph "why" trace
  - llm_model / tokens (optional)   → cost & repeatability

The framework writes the AgentRun for the subclass — the subclass cannot
forget. The same applies to BlackboardObject mixin fields
(object_type / created_by / created_at) — they are auto-stamped from
``self.agent_name``.

Usage:

    class MyAgent(BaseAgent):
        agent_name  = "Classification_Agent"
        stage       = "Classification"
        llm_model   = "gemma4:26b"

        def run(self, store: BlackboardStore) -> AgentResult:
            pes = store.load()["product_evidence_state"]
            self.read_input(pes["product_id"])
            # ... read core and cite ...
            self.cite("cn_hs8_pair_rows", "19023010", "...", "...")
            self.reason("Product matches 1902.30.10 because ...")
            # produce an output
            ccs_id = store.next_id("ccs")
            store.append("candidate_code_sets", {...})
            self.wrote(ccs_id)
            return AgentResult(success=True)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from agents.blackboard import BlackboardStore, now_iso


@dataclass
class AgentResult:
    """Lightweight return value from BaseAgent.execute()."""
    success: bool
    agent_run_id: str = ""
    error: str | None = None
    outputs_written: list[str] = field(default_factory=list)


class BaseAgent:
    """Abstract base — subclasses set class attrs and implement ``run``."""

    # ---- Subclass MUST set these ----
    agent_name: str = ""
    stage: str = ""

    # ---- Subclass MAY set these ----
    llm_model: str | None = None       # None for deterministic services

    def __init__(self):
        if not self.agent_name or not self.stage:
            raise ValueError(
                f"{type(self).__name__} must set class attrs agent_name and stage"
            )
        self._reset_trace()

    # ------------------------------------------------------------------ trace
    def _reset_trace(self) -> None:
        self._inputs_read: list[str] = []
        self._outputs_written: list[str] = []
        self._ontology_reads: list[dict] = []
        self._reasoning_chunks: list[str] = []
        self._prompt_excerpt: str | None = None
        self._llm_tokens_in: int = 0
        self._llm_tokens_out: int = 0

    def read_input(self, blackboard_object_id: str) -> None:
        """Record a Blackboard object id that this run read."""
        if blackboard_object_id and blackboard_object_id not in self._inputs_read:
            self._inputs_read.append(blackboard_object_id)

    def wrote(self, blackboard_object_id: str) -> None:
        """Record a Blackboard object id that this run wrote."""
        if blackboard_object_id and blackboard_object_id not in self._outputs_written:
            self._outputs_written.append(blackboard_object_id)

    def cite(
        self,
        source_table: str,
        source_id: str,
        snippet: str = "",
        reason: str = "",
        level: str | None = None,
    ) -> dict:
        """Append one EvidenceCitation to ontology_reads."""
        c: dict[str, Any] = {
            "source_table": source_table,
            "source_id": source_id,
        }
        if level is not None:
            c["level"] = level
        if snippet:
            c["snippet"] = snippet
        if reason:
            c["reason"] = reason
        self._ontology_reads.append(c)
        return c

    def reason(self, chunk: str) -> None:
        """Append a sentence/paragraph to reasoning_summary. Multiple calls join with space."""
        if chunk:
            self._reasoning_chunks.append(chunk.strip())

    def record_prompt(self, prompt_excerpt: str) -> None:
        """Store an admin-only prompt excerpt (truncated to 2000 chars)."""
        self._prompt_excerpt = (prompt_excerpt or "")[:2000]

    def record_tokens(self, tokens_in: int, tokens_out: int) -> None:
        self._llm_tokens_in += tokens_in
        self._llm_tokens_out += tokens_out

    # ------------------------------------------------------------------ exec
    def execute(self, store: BlackboardStore) -> AgentResult:
        """Wrap ``run`` with automatic AgentRun logging.

        Subclasses override ``run``; they do not write AgentRun themselves.
        """
        self._reset_trace()
        agent_run_id = store.next_id("ar")  # see [[agent-run-id-namespace]]
        started = now_iso()
        t0 = time.monotonic()
        error: str | None = None
        try:
            self.run(store)
            success = True
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
            success = False
        finished = now_iso()
        duration_ms = int((time.monotonic() - t0) * 1000)

        agent_run = {
            "object_type": "AgentRun",
            "created_by": self.agent_name,
            "created_at": started,
            "agent_run_id": agent_run_id,
            "agent_name": self.agent_name,
            "stage": self.stage,
            "inputs_read": list(self._inputs_read),
            "outputs_written": list(self._outputs_written),
            "ontology_reads": list(self._ontology_reads),
            "reasoning_summary": " ".join(self._reasoning_chunks) or None,
            "prompt_excerpt": self._prompt_excerpt,
            "started_at": started,
            "finished_at": finished,
            "duration_ms": duration_ms,
            "llm_model": self.llm_model,
            "llm_tokens_in": self._llm_tokens_in if self.llm_model else None,
            "llm_tokens_out": self._llm_tokens_out if self.llm_model else None,
            "error": error,
        }
        # Drop None values so the jsonl stays compact.
        agent_run = {k: v for k, v in agent_run.items() if v is not None}
        store.log_agent_run(agent_run)

        return AgentResult(
            success=success,
            agent_run_id=agent_run_id,
            error=error,
            outputs_written=list(self._outputs_written),
        )

    # ------------------------------------------------------------------ override
    def run(self, store: BlackboardStore) -> None:
        """Subclass entrypoint. Read inputs, cite core rows, reason, write outputs."""
        raise NotImplementedError

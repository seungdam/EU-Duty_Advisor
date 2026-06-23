"""
ASAP multi-agent Blackboard storage.

One pipeline run = one directory under ``data/runs/run_<NNN>/``:
  - ``blackboard.json``  — full mutable Blackboard root document.
  - ``agent_runs.jsonl`` — append-only log of AgentRun records.

The schema is defined in
``docs/ASAP_Ontology_v1/linkml/asap_runtime.yaml``
and the generated JSON Schema lives at
``docs/ASAP_Ontology_v1/linkml/generated/asap_runtime.schema.json``.
"""
from .store import BlackboardStore, now_iso

__all__ = ["BlackboardStore", "now_iso"]

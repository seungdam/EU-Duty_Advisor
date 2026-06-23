"""
Blackboard storage — one JSON document + one append-only jsonl per run.

Layout:
  data/runs/run_<NNN>/
    blackboard.json     full Blackboard root (mutable)
    agent_runs.jsonl    AgentRun records (append-only)

Schema validation is against the LinkML-generated JSON Schema
(docs/ASAP_Ontology_v1/linkml/generated/asap_runtime.schema.json).

Identifier format: prefix_counter, zero-padded to 3 digits
(cand_001, chg_001, …). Counters are derived from the live Blackboard so
nothing external needs to remember the next value.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Iterator

from bussiness_logic.app_config import LoadAppConfig

try:
    import jsonschema
except ImportError:  # pragma: no cover - optional runtime validation dependency
    jsonschema = None


PROJECT_ROOT = Path(os.environ.get("ASAP_PROJECT_ROOT", Path(__file__).resolve().parents[3])).resolve()
APP_CONFIG = LoadAppConfig(PROJECT_ROOT)
DEFAULT_RUNS_DIR = APP_CONFIG.paths.ResolvePath(
    PROJECT_ROOT,
    APP_CONFIG.paths.blackboard_runs_root,
)
DEFAULT_SCHEMA = APP_CONFIG.paths.ResolvePath(
    PROJECT_ROOT,
    APP_CONFIG.paths.blackboard_schema,
)


OBJECT_KEY: dict[str, str] = {
    "ProductEvidenceState": "product_evidence_state",
    "CandidateCodeSet": "candidate_code_sets",
    # Legacy draft object names retained for old run inspection only. Current
    # agents should fold this context into CandidateCodeSet / DocumentPackage.
    "TaricMeasureContext": "taric_measure_contexts",
    "DocumentPackage": "document_packages",
    # Legacy draft object name retained for old run inspection only. Current
    # domain routing is owned by Document_Agent and folded into DocumentPackage.
    "RegulatoryRoute": "regulatory_routes",
    "Challenge": "challenges",
    "ChallengeResponse": "challenge_responses",
    "OrchestratorDecision": "orchestrator_decisions",
    "UserQuestion": "user_questions",
}

AGENT_WRITE_KEYS: dict[str, set[str]] = {
    "Evidence_Intake_Agent": {"product_evidence_state", "user_questions"},
    "Classification_Agent": {"candidate_code_sets", "challenge_responses", "user_questions"},
    "Document_Agent": {"document_packages", "challenges", "user_questions"},
    "Orchestrator_Agent": {"orchestrator_decisions", "user_questions"},
}


def now_iso() -> str:
    """ISO-8601 timestamp with local timezone, seconds precision."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _next_run_id(runs_dir: Path) -> str:
    runs_dir.mkdir(parents=True, exist_ok=True)
    existing: list[int] = []
    for p in runs_dir.iterdir():
        if p.is_dir():
            m = re.match(r"^run_(\d+)$", p.name)
            if m:
                existing.append(int(m.group(1)))
    next_n = (max(existing) + 1) if existing else 1
    return f"run_{next_n:03d}"


class BlackboardStore:
    """Per-run Blackboard JSON + AgentRun jsonl I/O."""

    def __init__(
        self,
        run_id: str,
        runs_dir: Path = DEFAULT_RUNS_DIR,
        schema_path: Path = DEFAULT_SCHEMA,
        validate_on_write: bool | None = None,
    ):
        self.run_id = run_id
        self.run_dir = Path(runs_dir) / run_id
        self.bb_path = self.run_dir / "blackboard.json"
        self.runs_path = self.run_dir / "agent_runs.jsonl"
        self.schema_path = Path(schema_path)
        self._schema: dict | None = None
        self._counters: dict[str, int] = {}
        self.validate_on_write = (
            os.environ.get("ASAP_VALIDATE_BLACKBOARD_ON_WRITE") == "1"
            if validate_on_write is None
            else validate_on_write
        )

    # ------------------------------------------------------------------ create
    @classmethod
    def create(
        cls,
        *,
        origin_country: str = "KR",
        destination_market: str = "EU",
        language: str = "ko",
        runtime_mode: str = "prototype",
        reference_date: str | None = None,
        runs_dir: Path = DEFAULT_RUNS_DIR,
        schema_path: Path = DEFAULT_SCHEMA,
        validate_on_write: bool | None = None,
    ) -> "BlackboardStore":
        """Create a fresh run directory + empty Blackboard root."""
        runs_dir = Path(runs_dir)
        run_id = _next_run_id(runs_dir)
        store = cls(
            run_id,
            runs_dir=runs_dir,
            schema_path=schema_path,
            validate_on_write=validate_on_write,
        )
        store.run_dir.mkdir(parents=True, exist_ok=True)
        n = run_id.split("_", 1)[1]
        bb = {
            "blackboard_id": f"bb_{n}",
            "run_context": {
                "run_id": run_id,
                "created_at": now_iso(),
                "origin_country": origin_country,
                "destination_market": destination_market,
                "reference_date": reference_date
                or datetime.now(timezone.utc).date().isoformat(),
                "language": language,
                "runtime_mode": runtime_mode,
            },
            "candidate_code_sets": [],
            "taric_measure_contexts": [],
            "document_packages": [],
            "regulatory_routes": [],
            "challenges": [],
            "challenge_responses": [],
            "orchestrator_decisions": [],
            "user_questions": [],
            "agent_runs": [],
        }
        store.save(bb)
        store.runs_path.touch()
        return store

    # ------------------------------------------------------------------ io
    def load(self) -> dict:
        if not self.bb_path.exists():
            raise FileNotFoundError(f"blackboard not found: {self.bb_path}")
        return json.loads(self.bb_path.read_text(encoding="utf-8"))

    def save(self, bb: dict) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if self.validate_on_write:
            self.validate(bb)
        self.bb_path.write_text(
            json.dumps(bb, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------ ids
    def next_id(self, prefix: str) -> str:
        """Allocate ``{prefix}_NNN`` — counter scanned from the live document.

        The scan is performed lazily on first call per prefix; subsequent
        calls just increment the in-memory counter.
        """
        if prefix not in self._counters:
            bb = self.load() if self.bb_path.exists() else {}
            self._counters[prefix] = self._scan_highest(bb, prefix)
        self._counters[prefix] += 1
        return f"{prefix}_{self._counters[prefix]:03d}"

    @staticmethod
    def _scan_highest(obj: Any, prefix: str) -> int:
        pat = re.compile(rf"^{re.escape(prefix)}_(\d+)$")
        highest = 0

        def walk(node: Any) -> None:
            nonlocal highest
            if isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
            elif isinstance(node, str):
                m = pat.match(node)
                if m:
                    highest = max(highest, int(m.group(1)))

        walk(obj)
        return highest

    # ------------------------------------------------------------------ mutate
    def put(self, key: str, obj: dict) -> dict:
        """Overwrite ``bb[key]`` (single-instance slot like product_evidence_state)."""
        self._enforce_write(key, obj)
        bb = self.load()
        bb[key] = obj
        self.save(bb)
        return obj

    def append(self, key: str, obj: dict) -> dict:
        """Append ``obj`` to ``bb[key]`` (multivalued slot)."""
        self._enforce_write(key, obj)
        bb = self.load()
        bb.setdefault(key, []).append(obj)
        self.save(bb)
        return obj

    @staticmethod
    def _enforce_write(key: str, obj: dict) -> None:
        """Enforce the v2 Agent Read/Write Matrix for Blackboard writes."""
        object_type = obj.get("object_type")
        created_by = obj.get("created_by")

        expected_key = OBJECT_KEY.get(object_type or "")
        if expected_key and expected_key != key:
            raise PermissionError(
                f"{object_type} must be written to {expected_key!r}, not {key!r}"
            )

        if not created_by:
            raise PermissionError(f"Blackboard object written to {key!r} is missing created_by")

        allowed = AGENT_WRITE_KEYS.get(created_by)
        if allowed is None:
            raise PermissionError(f"Unknown Blackboard writer: {created_by!r}")
        if key not in allowed:
            raise PermissionError(
                f"{created_by} may not write {key!r}; allowed keys: {sorted(allowed)}"
            )

    # ------------------------------------------------------------------ agent run log
    def log_agent_run(self, agent_run: dict) -> dict:
        """Append AgentRun to the per-run jsonl. Also mirror its id in
        Blackboard.agent_runs for fast indexing."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with self.runs_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(agent_run, ensure_ascii=False) + "\n")
        bb = self.load()
        bb.setdefault("agent_runs", []).append(agent_run.get("agent_run_id"))
        self.save(bb)
        return agent_run

    def iter_agent_runs(self) -> Iterator[dict]:
        if not self.runs_path.exists():
            return
        with self.runs_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)

    # ------------------------------------------------------------------ validate
    def _load_schema(self) -> dict:
        if self._schema is None:
            self._schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        return self._schema

    def validate(self, bb: dict | None = None) -> None:
        """Validate against the LinkML-generated JSON Schema.

        Raises ``jsonschema.ValidationError`` on the first rule violation.
        """
        if jsonschema is None:
            raise RuntimeError("jsonschema is not installed; cannot validate Blackboard")
        bb = bb if bb is not None else self.load()
        jsonschema.validate(instance=bb, schema=self._load_schema())

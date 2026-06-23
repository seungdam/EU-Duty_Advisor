"""
Admin viewer — read-only CLI inspection of a run's Blackboard + AgentRun log.

ADMIN ONLY. End-users must not see this output. Renders the agent trace,
core citations, reasoning summaries, challenges, and final orchestrator
decision so an operator can answer "why did the system decide X."

Usage:
    python -m blackboard.viewer list
    python -m blackboard.viewer show run_001
    python -m blackboard.viewer timeline run_001
    python -m blackboard.viewer citations run_001 [--agent Classification_Agent]
    python -m blackboard.viewer object  run_001 cand_001
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .store import BlackboardStore, DEFAULT_RUNS_DIR


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def list_runs(runs_dir: Path = DEFAULT_RUNS_DIR) -> list[str]:
    if not runs_dir.exists():
        return []
    return sorted(
        p.name for p in runs_dir.iterdir()
        if p.is_dir() and (p / "blackboard.json").exists()
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_list(args: argparse.Namespace) -> int:
    runs = list_runs(args.runs_dir)
    if not runs:
        print(f"(no runs in {args.runs_dir})")
        return 0
    for run_id in runs:
        store = BlackboardStore(run_id, runs_dir=args.runs_dir)
        try:
            bb = store.load()
            rc = bb.get("run_context", {})
            n_cands = sum(len(c.get("candidates", [])) for c in bb.get("candidate_code_sets", []))
            n_decs = len(bb.get("orchestrator_decisions", []))
            print(f"  {run_id}  origin={rc.get('origin_country','?')}  "
                  f"created={rc.get('created_at','?')[:19]}  "
                  f"candidates={n_cands}  decisions={n_decs}")
        except Exception as e:  # noqa: BLE001
            print(f"  {run_id}  [load error: {e}]")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = BlackboardStore(args.run_id, runs_dir=args.runs_dir)
    bb = store.load()
    rc = bb.get("run_context", {})
    print(f"=== {args.run_id} ===")
    print(f"created_at:        {rc.get('created_at')}")
    print(f"origin → market:   {rc.get('origin_country')} → {rc.get('destination_market')}")
    print(f"language:          {rc.get('language')}")
    print(f"runtime_mode:      {rc.get('runtime_mode')}")
    print()

    pes = bb.get("product_evidence_state") or {}
    if pes:
        obs = pes.get("observed_facts", {})
        print(f"ProductEvidenceState  {pes['product_id']}")
        print(f"  product_name:  {obs.get('product_name')}")
        print(f"  description:   {obs.get('description')}")
        print(f"  origin:        {obs.get('origin_country')}")
        if pes.get("inferred_facts"):
            print(f"  inferred:")
            for f in pes["inferred_facts"]:
                print(f"    - {f['fact_key']}={f['value']} "
                      f"(conf={f.get('confidence')}, status={f.get('status')})")
        print()

    for ccs in bb.get("candidate_code_sets", []):
        print(f"CandidateCodeSet  {ccs['candidate_set_id']}  by {ccs['created_by']}")
        for c in ccs["candidates"]:
            print(f"  └─ {c['candidate_id']:11s} hs6={c.get('hs6')} cn8={c.get('cn8')} "
                  f"taric10={c.get('taric10')} rank={c.get('rank')} "
                  f"conf={c.get('confidence')} status={c.get('status')}")
            for b in c.get("classification_basis", []):
                print(f"     · {b}")
        print()

    for tmc in bb.get("taric_measure_contexts", []):
        print(f"TaricMeasureContext  {tmc['context_id']} → {tmc['candidate_id']}  "
              f"measures={len(tmc.get('measure_rows', []))}  status={tmc.get('retrieval_status')}")
    if bb.get("taric_measure_contexts"):
        print()

    for dp in bb.get("document_packages", []):
        print(f"DocumentPackage  {dp['document_package_id']} → {dp['candidate_id']}  "
              f"reqs={len(dp.get('requirements', []))}")
        for r in dp.get("requirements", []):
            print(f"  └─ {r['requirement_id']}  type={r['requirement_type']}  "
                  f"required={r['required']}  evidence={r['evidence_status']}")
    if bb.get("document_packages"):
        print()

    for rr in bb.get("regulatory_routes", []):
        print(f"RegulatoryRoute  {rr['regulatory_route_id']} → {rr['candidate_id']}")
        for dr in rr.get("domain_routes", []):
            print(f"  └─ {dr['domain']}/{dr['route']}  status={dr['status']}  "
                  f"reason={dr.get('reason')}")
    if bb.get("regulatory_routes"):
        print()

    for ch in bb.get("challenges", []):
        print(f"Challenge  {ch['challenge_id']}  by {ch['raised_by']}  "
              f"severity={ch['severity']}  status={ch['status']}")
        print(f"  type={ch['challenge_type']}  issue={ch.get('issue','')}")
    if bb.get("challenges"):
        print()

    for dec in bb.get("orchestrator_decisions", []):
        print(f"OrchestratorDecision  {dec['decision_id']}  "
              f"status={dec['decision_status']}")
        print(f"  selected:  {dec.get('selected_candidate_ids')}")
        print(f"  reason:    {dec.get('reason','')}")

    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    store = BlackboardStore(args.run_id, runs_dir=args.runs_dir)
    print(f"=== {args.run_id} agent timeline ===\n")
    for r in store.iter_agent_runs():
        print(f"[{r['started_at'][11:19]}] {r['agent_run_id']}  {r['agent_name']}  "
              f"({r.get('duration_ms','?')}ms)")
        if r.get("stage"):
            print(f"  stage:    {r['stage']}")
        if r.get("inputs_read"):
            print(f"  reads:    {r['inputs_read']}")
        if r.get("outputs_written"):
            print(f"  writes:   {r['outputs_written']}")
        if r.get("ontology_reads"):
            print(f"  cites:    {len(r['ontology_reads'])} core row(s):")
            for c in r["ontology_reads"]:
                lvl = f" lvl={c['level']}" if c.get("level") else ""
                print(f"            · {c['source_table']}/{c['source_id']}{lvl}")
                if c.get("snippet"):
                    print(f"              \"{c['snippet'][:90]}\"")
                if c.get("reason"):
                    print(f"              → {c['reason']}")
        if r.get("reasoning_summary"):
            print(f"  reason:   {r['reasoning_summary']}")
        if r.get("llm_model"):
            ti = r.get("llm_tokens_in") or 0
            to = r.get("llm_tokens_out") or 0
            print(f"  llm:      {r['llm_model']}  tokens={ti}/{to}")
        if r.get("error"):
            print(f"  ERROR:    {r['error']}")
        print()
    return 0


def cmd_citations(args: argparse.Namespace) -> int:
    store = BlackboardStore(args.run_id, runs_dir=args.runs_dir)
    by_source: dict[str, list[tuple[str, dict]]] = {}
    for r in store.iter_agent_runs():
        if args.agent and r.get("agent_name") != args.agent:
            continue
        for c in r.get("ontology_reads", []):
            by_source.setdefault(c["source_table"], []).append((r["agent_name"], c))
    if not by_source:
        print("(no citations)")
        return 0
    for table in sorted(by_source):
        print(f"== {table} ({len(by_source[table])} citation(s)) ==")
        for agent_name, c in by_source[table]:
            lvl = f" lvl={c['level']}" if c.get("level") else ""
            print(f"  {agent_name:30s} {c['source_id']}{lvl}")
            if c.get("snippet"):
                print(f"    \"{c['snippet'][:120]}\"")
            if c.get("reason"):
                print(f"    → {c['reason']}")
        print()
    return 0


def cmd_object(args: argparse.Namespace) -> int:
    """Dump one object by id from anywhere in the Blackboard."""
    store = BlackboardStore(args.run_id, runs_dir=args.runs_dir)
    bb = store.load()
    target = args.object_id

    def walk(node: Any) -> dict | None:
        if isinstance(node, dict):
            for key in ("blackboard_id", "product_id", "candidate_set_id",
                        "candidate_id", "context_id", "measure_id",
                        "document_package_id", "requirement_id",
                        "regulatory_route_id", "challenge_id",
                        "response_id", "decision_id", "user_question_id"):
                if node.get(key) == target:
                    return node
            for v in node.values():
                hit = walk(v)
                if hit is not None:
                    return hit
        elif isinstance(node, list):
            for v in node:
                hit = walk(v)
                if hit is not None:
                    return hit
        return None

    obj = walk(bb)
    if obj is None:
        # also check agent_runs.jsonl
        for r in store.iter_agent_runs():
            if r.get("agent_run_id") == target:
                obj = r
                break
    if obj is None:
        print(f"(not found: {target})")
        return 1
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m blackboard.viewer",
        description="Admin viewer for ASAP Blackboard runs. Not for end-users.",
    )
    p.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR,
                   help="Override runs directory.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List runs.").set_defaults(func=cmd_list)

    sp = sub.add_parser("show", help="Show full Blackboard contents.")
    sp.add_argument("run_id")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("timeline", help="Show AgentRun timeline with citations + reasoning.")
    sp.add_argument("run_id")
    sp.set_defaults(func=cmd_timeline)

    sp = sub.add_parser("citations", help="Group core citations by source table.")
    sp.add_argument("run_id")
    sp.add_argument("--agent", default=None)
    sp.set_defaults(func=cmd_citations)

    sp = sub.add_parser("object", help="Dump one object by id.")
    sp.add_argument("run_id")
    sp.add_argument("object_id")
    sp.set_defaults(func=cmd_object)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

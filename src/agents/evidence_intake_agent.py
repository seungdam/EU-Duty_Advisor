"""
Evidence_Intake_Agent — stub.

Reads raw user input (product name, description, optional OCR text, source
URLs) and writes a ProductEvidenceState onto the Blackboard.

This stub does *not* call an LLM. A real implementation would parse OCR
output, extract composition tables, and normalize ingredient terms.
"""
from __future__ import annotations

from typing import Any

from agents.agent_base import BaseAgent
from agents.blackboard import BlackboardStore, now_iso


class EvidenceIntakeAgent(BaseAgent):
    agent_name = "Evidence_Intake_Agent"
    stage = "Product_Intake"
    llm_model = None  # deterministic intake; flip to a model when OCR/LLM is wired

    def __init__(self, raw_input: dict[str, Any]):
        super().__init__()
        self.raw_input = raw_input

    def run(self, store: BlackboardStore) -> None:
        prod_id = store.next_id("prod")
        obs = {
            "product_name": self.raw_input.get("product_name", ""),
            "description": self.raw_input.get("description", ""),
            "composition": self.raw_input.get("composition", []),
            "classification_input_product_facts": self.raw_input.get(
                "classification_input_product_facts",
                [],
            ),
            "unresolved_product_facts": self.raw_input.get(
                "unresolved_product_facts",
                [],
            ),
            "product_fact_conflicts": self.raw_input.get(
                "product_fact_conflicts",
                [],
            ),
            "classification_input_fact_texts": self.raw_input.get(
                "classification_input_fact_texts",
                [],
            ),
            "input_reconstruction": self.raw_input.get("input_reconstruction", {}),
            "ocr_text": self.raw_input.get("ocr_text", []),
            "source_urls": self.raw_input.get("source_urls", []),
            "origin_country": self.raw_input.get("origin_country", "KR"),
            "intended_use": self.raw_input.get("intended_use", "unknown"),
            "warnings": self.raw_input.get("warnings", []),
        }

        # Stub-level inferred facts: a single proposed principal_form when the
        # name string hints at one. A real agent would do much more.
        inferred: list[dict] = []
        name = (obs["product_name"] or "").lower()
        for kw, form in [("noodle", "pasta_dry"), ("ramen", "pasta_dry"),
                         ("라면", "pasta_dry"), ("cream", "cosmetic_cream"),
                         ("lotion", "cosmetic_lotion")]:
            if kw in name:
                inferred.append({
                    "fact_key": "principal_form",
                    "value": form,
                    "confidence": 0.5,
                    "evidence": [],
                    "status": "proposed",
                })
                self.reason(f"Detected token '{kw}' in product_name; proposed principal_form={form}.")
                break

        pes = {
            "object_type": "ProductEvidenceState",
            "created_by": self.agent_name,
            "created_at": now_iso(),
            "product_id": prod_id,
            "observed_facts": obs,
            "inferred_facts": inferred,
            "unknowns": ["composition_pct" if not obs["composition"] else "",
                         "intended_use" if obs["intended_use"] == "unknown" else ""],
            "evidence_pointers": [],
        }
        pes["unknowns"] = [u for u in pes["unknowns"] if u]
        store.put("product_evidence_state", pes)
        self.wrote(prod_id)
        self.reason(f"Created ProductEvidenceState {prod_id} from raw input.")

"""Kurly product facts collection helpers."""

from __future__ import annotations

from collections.abc import Callable

from bussiness_logic.input_process.product_facts import NormalizeProductFacts
from bussiness_logic.utils.json_types import JsonObject


def CollectKurlyProductFactsIfNeeded(
    *,
    facts: JsonObject,
    imageStatusCallback: Callable[[list[JsonObject]], None] | None = None,
    runId: str | None = None,
) -> JsonObject:
    """Collect Kurly product facts when the normalized input only has a URL."""
    normalizedFacts = NormalizeProductFacts(facts or {})
    explicitUserInputFacts = normalizedFacts.get("user_input_facts")
    url = str(normalizedFacts.get("url") or "").strip()
    if (
        url
        and not _HasCollectedKurlyFacts(normalizedFacts)
        and (
            "kurly.com/goods/" in url
            or "kurlyglobal.com/products/" in url
            or "kurlyglobal.com/en/products/" in url
        )
    ):
        try:
            from bussiness_logic.product.pipeline.kurly_url_facts import (
                CollectKurlyUrlFacts,
            )

            collected = CollectKurlyUrlFacts(
                url,
                imageStatusCallback=imageStatusCallback,
                runId=runId,
            )
            merged = dict(normalizedFacts)
            for key, value in collected.items():
                if value not in ("", [], None):
                    merged[key] = value
            if isinstance(explicitUserInputFacts, dict) and explicitUserInputFacts:
                merged["user_input_facts"] = explicitUserInputFacts
            normalizedFacts = NormalizeProductFacts(merged)
        except Exception as exc:  # noqa: BLE001
            normalizedFacts.setdefault("warnings", [])
            normalizedFacts["warnings"].append(f"kurly_url_intake_failed: {exc}")
    return normalizedFacts


def _HasCollectedKurlyFacts(facts: JsonObject) -> bool:
    inputReconstruction = facts.get("input_reconstruction") or {}
    if not isinstance(inputReconstruction, dict):
        inputReconstruction = {}
    return bool(
        facts.get("reconstructed_product_facts")
        or facts.get("reconstructed_fact_texts")
        or inputReconstruction.get("reconstructed_product_facts")
        or inputReconstruction.get("reconstructed_fact_texts")
        or facts.get("url_intake")
    )

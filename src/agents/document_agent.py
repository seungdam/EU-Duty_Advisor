"""
Document_Agent — candidate TARIC10 branch(es) → 5-section document package,
regulatory domain, and backtracking signals.

Owned tools:
  - DocumentPackageTool   (taric10 → raw measures/certs/duty/CELEX)
  - DomainRouterTool      (cn8 + product facts → regulatory domain)

Output schema (Blackboard.document_packages, schema unconfirmed — minimal
fields for now):

  {
    "document_package_id": "dp_001",
    "candidate_id": "cand_001",
    "cn8": "19023010", "taric10": "1902301010",
    "customs_check_items":      [...],   # 세관확인사항 (control measures)
    "basic_duty":               {...},   # 기본관세 (Third country duty)
    "preferential_evidence":    [...],   # 우대증빙 (FTA / preferential origin)
    "required_documents":       [...],   # 요구서류 (C/N/L-class certs)
    "product_regulations":      [...],   # 제품규제 (domain-derived)
    "missing_facts":            [...],
    "external_lookup":          [...],
    "celex_basis":              [...],
    "backtracking_signals":     [...],   # → Classification 재검토 신호
  }

LLM 호출 없음 — current MVP. CELEX 본문 해석/카드 생성 LLM 통합은 후속.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from agents.agent_base import BaseAgent
from agents.tools import DocumentPackageTool, DomainRouterTool
from agents.blackboard import BlackboardStore, now_iso


# ---------------------------------------------------------------------------
# Measure type → section bucket
# ---------------------------------------------------------------------------
_CONTROL_KEYWORDS = (
    "Veterinary", "CITES", "GMO", "Phytosanitary",
    "Luxury", "Sanction", "fishing", "Import control",
    "Export control", "Surveillance",
)
_DUTY_KEYWORDS = (
    "Third country duty",
    "Customs Union Duty",
    "Autonomous tariff",
    "Supplementary unit",
)
_PREFERENTIAL_KEYWORDS = (
    "Tariff preference",
    "Customs Union",
    "Preferential tariff quota",
    "Preferential",
)


def _classify_measure(measure_type: str) -> str:
    """One of: customs_check / basic_duty / preferential / other."""
    mt = measure_type or ""
    if any(k in mt for k in _CONTROL_KEYWORDS):
        return "customs_check"
    if any(k in mt for k in _PREFERENTIAL_KEYWORDS):
        return "preferential"
    if any(k in mt for k in _DUTY_KEYWORDS):
        return "basic_duty"
    return "other"


# ---------------------------------------------------------------------------
# Certificate prefix → document classification
# (재사용: agents/document_package.cert_category 와 동일 의미)
# ---------------------------------------------------------------------------
def _cert_kind(code: str) -> str:
    code = (code or "").strip().upper()
    if code.startswith("Y"):
        return "exemption_declaration"
    if code.startswith("C"):
        return "mandatory_certificate"
    if code.startswith("N"):
        return "national_document"
    if code.startswith("U"):
        return "preferential_origin"
    if code.startswith("L"):
        return "import_license"
    return "other"


def _compact_text(value: Any) -> str:
    return str(value or "").strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "t", "1", "yes", "y"}


def _first_text(*values: Any) -> str:
    for value in values:
        text = _compact_text(value)
        if text:
            return text
    return ""


def _enrich_baseline_doc(detail: dict[str, Any]) -> dict[str, Any]:
    """Map DetailedRequirement dict → UI-friendly baseline_document.

    render_document_checklist expects these flat keys:
      document_name_ko / document_name / document_code
      prepared_by / submitted_to
      taric_certificates / pre_checks / post_requirements
      fields  (list of {field_key, label, required_by, missing_facts, status})
      decision_status / required_level / missing_facts
    """
    cond = detail.get("condition_text") or ""
    parsed: dict[str, str] = {}
    for part in cond.split("|"):
        part = part.strip()
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        parsed[k.strip()] = v.strip()

    def _split(value: str) -> list[str]:
        if not value:
            return []
        return [item.strip() for item in value.split(";") if item.strip()]

    document_name = detail.get("required_document") or detail.get("requirement_master_id") or "Baseline document"
    document_code = parsed.get("document_code") or detail.get("requirement_master_id") or ""

    field_keys = detail.get("user_fallback_evidence") or []
    missing_set = set(detail.get("missing_facts") or [])
    fields = [
        {
            "field_key": key,
            "label": key.replace("_", " "),
            "required_by": ["baseline"],
            "missing_facts": [key] if key in missing_set else [],
            "status": "conditional" if key in missing_set else "satisfied",
        }
        for key in field_keys[:12]
    ]

    enriched = dict(detail)
    enriched.update({
        "document_code": document_code,
        "document_name": document_name,
        "document_name_ko": document_name,
        "prepared_by": parsed.get("prepared_by", ""),
        "submitted_to": parsed.get("submitted_to", ""),
        "taric_certificates": (
            _split(parsed.get("linked_certificates", ""))
            or list(detail.get("external_dataset_ids") or [])
        ),
        "pre_checks": [
            {"requirement_type": t} for t in _split(parsed.get("linked_pre", ""))
        ],
        "post_requirements": [
            {"requirement_type": t} for t in _split(parsed.get("linked_post", ""))
        ],
        "fields": fields,
    })
    return enriched


def _view_is_control_measure(req: dict[str, Any]) -> bool:
    mt = req.get("measure_type") or ""
    return any(
        k in mt
        for k in (
            "Import control",
            "Import restriction",
            "Veterinary",
            "CITES",
            "GMO",
            "Phytosanitary",
            "REACH",
        )
    ) or any(
        k in mt.lower()
        for k in ("fishing", "luxury", "sanction", "restriction", "surveillance", "control")
    )


def _view_is_duty_measure(req: dict[str, Any]) -> bool:
    mt = req.get("measure_type") or ""
    return any(k in mt for k in ("duty", "Duty", "Tariff", "Preference", "Preferential", "Customs Union", "Supplementary"))


def _view_is_preferential_measure(req: dict[str, Any]) -> bool:
    mt = req.get("measure_type") or ""
    return any(k in mt for k in ("Tariff preference", "Customs Union", "Preferential"))


def _view_is_base_duty_measure(req: dict[str, Any]) -> bool:
    mt = req.get("measure_type") or ""
    if _view_is_preferential_measure(req):
        return False
    return any(k in mt for k in ("Third country duty", "Additional duties", "Supplementary unit", "duty", "Duty"))


def _view_find_measure(measures: list[dict[str, Any]], needles: tuple[str, ...]) -> dict[str, Any] | None:
    return next((m for m in measures if any(n in (m.get("measure_type") or "") for n in needles)), None)


def _domain_aliases(domain: str) -> set[str]:
    aliases = {
        "animal_origin_food": {"animal_origin", "fishery"},
        "animal_origin": {"animal_origin_food", "fishery"},
        "fishery": {"animal_origin", "animal_origin_food"},
        "cites": {"cites"},
        "organic": {"organic", "food_feed_non_animal", "animal_origin_food"},
        "plant_health": {"plant_health", "food_feed_non_animal"},
        "food_feed_non_animal": {"organic", "plant_health"},
    }
    return {domain, *aliases.get(domain, set())} if domain else set()


def _declaration_label(cert: dict[str, Any]) -> str:
    code = (cert.get("code") or "").upper()
    guidance = cert.get("guidance") or {}
    title = _first_text(guidance.get("guidance_title"), guidance.get("certificate_description"), cert.get("description"))
    return f"{code} {title}".strip() if title else code


def _related_declarations_by_domain(product_reqs: list[dict[str, Any]], kr_reqs: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Group Y-code exemption declarations by domain for the UI declaration panel.

    The UI looks declarations up by each rendered detail's own domain
    (``related_declarations.get(detail.domain_route)``). A measure-level
    declaration (e.g. a Veterinary-control Y-code on fishery/animal_origin_food)
    must therefore be surfaced under its OWN domain — not only under the routed
    product domain — otherwise a coarse route like ``food`` drops every
    declaration and the panel goes empty. We surface each declaration under its
    own domain AND under any routed product domain it aliases to. Exemption
    declarations are waivers the importer self-declares, so over-surfacing is
    safer than silently dropping them.
    """
    product_domains = {
        d.get("domain_route") or d.get("domain") or ""
        for req in product_reqs
        for d in (req.get("detailed_requirements") or [])
    }
    product_domains = {d for d in product_domains if d}

    cert_by_code = {
        (cert.get("code") or "").upper(): cert
        for req in kr_reqs
        for cert in (req.get("certificates") or [])
        if cert.get("code")
    }
    out: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()

    def _add(domain_key: str, label: str) -> None:
        if not domain_key:
            return
        key = (domain_key, label)
        if key in seen:
            return
        seen.add(key)
        out.setdefault(domain_key, []).append(label)

    for req in kr_reqs:
        if req.get("measure_type") == "Product regulatory requirements":
            continue
        for detail in req.get("detailed_requirements") or []:
            detail_domain = detail.get("domain_route") or detail.get("domain") or ""
            code = (detail.get("trigger_certificate_code") or "").upper()
            cert = cert_by_code.get(code)
            if not code or not cert:
                continue
            is_declaration = (
                code.startswith("Y")
                or cert.get("category") == "exemption_declaration"
                or "exemption declaration" in (detail.get("required_document") or "").lower()
            )
            if not is_declaration:
                continue
            label = _declaration_label(cert)
            # Always surface under the declaration's own domain (UI lookup key),
            # plus any routed product domain it aliases to.
            _add(detail_domain or "general", label)
            for product_domain in product_domains:
                if detail_domain in _domain_aliases(product_domain):
                    _add(product_domain, label)
    return out


# ---------------------------------------------------------------------------
# Document_Agent
# ---------------------------------------------------------------------------
class DocumentAgent(BaseAgent):
    agent_name = "Document_Agent"
    stage = "Document_Recommendation"
    llm_model = None  # MVP: deterministic. CELEX 해석 LLM 후속.

    def __init__(self, *, include_celex_excerpt: bool = False) -> None:
        super().__init__()
        self._doc_tool = DocumentPackageTool(include_celex_excerpt=include_celex_excerpt)
        self._domain_tool = DomainRouterTool()

    def run(self, store: BlackboardStore) -> None:
        bb = store.load()
        pes = bb.get("product_evidence_state") or {}
        ccs_list = bb.get("candidate_code_sets") or []
        if not pes:
            raise RuntimeError("Document_Agent requires a ProductEvidenceState.")
        if not ccs_list:
            self.reason("No CandidateCodeSet present; nothing to package.")
            return
        latest = ccs_list[-1]
        self.read_input(latest["candidate_set_id"])

        product_facts = pes.get("observed_facts") or {}
        routing_context = bb.get("routing_context") or {}
        if routing_context.get("routing_context_id"):
            self.read_input(routing_context["routing_context_id"])

        for cand in latest["candidates"]:
            self.read_input(cand["candidate_id"])
            cn8 = (cand.get("cn8") or "").strip()
            taric_targets = self._taric_targets_for_candidate(cand)

            # Sentinel: needs_more_facts candidates → no package, only signal.
            if (cand.get("status") or "").strip() != "proposed" or not taric_targets:
                reason = "candidate_unresolved"
                if (cand.get("status") or "").strip() == "proposed" and not taric_targets:
                    reason = "no_declarable_taric10_branch"
                self._emit_unresolved_package(store, cand, reason=reason)
                continue

            for target in taric_targets:
                taric10 = target["taric10"]
                cand_for_target = {**cand, "taric10": taric10}

                # 0. Prefer pre-classification RoutingContext for baseline /
                # pre-TARIC scope. Keep chapter tied to the candidate CN8 so a
                # candidate document package remains candidate-specific.
                route_domains = list(routing_context.get("domain_scopes") or [])
                route_pre_gates = list(routing_context.get("pre_gate_domains") or [])
                if route_domains or route_pre_gates:
                    product_facts_for_doc = {
                        **product_facts,
                        "cn8": cn8,
                        "taric10": taric10,
                        "regulatory_domains": route_domains,
                        "domain_scopes": route_domains,
                        "pre_gate_domains": route_pre_gates,
                        "chapter": cn8[:2],
                        "routing_context": routing_context,
                    }
                    self.reason(
                        f"Using RoutingContext for candidate {cand.get('candidate_id')}: "
                        f"chapter={cn8[:2]} domains={route_domains} pre_gate={route_pre_gates}."
                    )
                else:
                    pre_dom = self._domain_tool.route(
                        cn8=cn8, product_facts=product_facts, measure_type_hints=[],
                    )
                    product_facts_for_doc = {
                        **product_facts,
                        "cn8": cn8,
                        "taric10": taric10,
                        "regulatory_domains": list(pre_dom.domains),
                        "domain_scopes": list(pre_dom.domains),
                        "pre_gate_domains": list(getattr(pre_dom, "pre_gate_domains", []) or []),
                        "chapter": getattr(pre_dom, "chapter", "") or cn8[:2],
                    }

                # 1. Raw TARIC measure package
                try:
                    raw = self._doc_tool.resolve(taric10=taric10, product_facts=product_facts_for_doc)
                except Exception as e:  # noqa: BLE001
                    self.reason(f"DocumentPackageTool error for {taric10}: {e}")
                    self._emit_unresolved_package(store, cand_for_target, reason=f"tool_error: {e}")
                    continue

                requirements_raw = raw.get("requirements") or []
                self.cite(
                    "taric_master_table",
                    f"goods_code_10={taric10}",
                    snippet=f"{raw.get('total_measure_rows')} measure rows / "
                            f"{len(requirements_raw)} requirement groups",
                    reason="DocumentPackageTool source.",
                )

                # 2. Regulatory domain. Prefer upstream RoutingContext so
                # baseline/pre-TARIC scope is not silently recomputed from CN8.
                measure_hints = [r.get("measure_type") for r in requirements_raw if r.get("measure_type")]
                dom = (
                    self._domain_result_from_routing_context(routing_context, cn8)
                    or self._domain_tool.route(
                        cn8=cn8, product_facts=product_facts, measure_type_hints=measure_hints,
                    )
                )
                for ev in dom.evidence:
                    self.cite("Domain_Scope_Routes", str(ev.get("chapter") or ev.get("source") or ""),
                              snippet=str(ev)[:100], reason="DomainRouterTool evidence.")

                # 3. Bucket measures into 5 sections
                customs, duties, preferential = self._bucket_requirements(requirements_raw)

                required_documents = self._extract_required_documents(requirements_raw)
                product_regulations = self._build_product_regulations(dom)
                basic_duty = self._pick_basic_duty(duties, raw)
                document_view = self._build_document_view(
                    raw=raw,
                    dom=dom,
                    customs=customs,
                    duties=duties,
                    preferential=preferential,
                    required_documents=required_documents,
                    product_regulations=product_regulations,
                    basic_duty=basic_duty,
                )

                # 4. CELEX basis (collect all legal bases referenced)
                celex_basis = self._collect_celex(requirements_raw)

                # 5. Backtracking signals
                backtracking = self._backtracking_signals(cand_for_target, raw, dom)

                # 6. Missing facts
                missing_facts = list(dom.missing_facts)
                if not requirements_raw:
                    missing_facts.append("no_taric_requirements_found")
                if dom.is_ambiguous:
                    missing_facts.append("regulatory_domain_ambiguous")

                dp_id = store.next_id("dp")
                dp = {
                    "object_type": "DocumentPackage",
                    "created_by": self.agent_name,
                    "created_at": now_iso(),
                    "document_package_id": dp_id,
                    "candidate_id": cand["candidate_id"],
                    "cn8": cn8,
                    "taric10": taric10,
                    "taric10_branch": target.get("branch"),
                    "taric10_branch_index": target.get("branch_index"),
                    "taric10_branch_count": target.get("branch_count"),
                    "taric10_resolution_mode": target.get("resolution_mode"),
                    "taric10_is_recommended": False,

                    "customs_check_items": customs,
                    "basic_duty": basic_duty,
                    "preferential_evidence": preferential,
                    "required_documents": required_documents,
                    "product_regulations": product_regulations,
                    "document_view": document_view,
                    "raw_document_package": raw,

                    "missing_facts": missing_facts,
                    "external_lookup": self._suggest_external(dom),
                    "celex_basis": celex_basis,
                    "backtracking_signals": backtracking,

                    "summary": {
                        "duty": (basic_duty or {}).get("rate") or "see preferential",
                        "main_requirements": [d["title"] for d in required_documents[:5]],
                        "domains": dom.domains,
                        "unknowns": list(missing_facts),
                        "view_counts": document_view.get("metrics") or {},
                    },
                    "conflicts": [],
                }
                store.append("document_packages", dp)
                self.wrote(dp_id)
                branch_label = (
                    f"branch {target.get('branch_index')}/{target.get('branch_count')}"
                    if target.get("branch_count", 0) > 1
                    else "single branch"
                )
                self.reason(
                    f"DocumentPackage {dp_id} for cand={cand['candidate_id']} "
                    f"({taric10}, {branch_label}): customs={len(customs)} duties={len(duties)} "
                    f"pref={len(preferential)} reqs={len(required_documents)} "
                    f"product_rules={document_view.get('metrics', {}).get('product_rule_count', 0)} "
                    f"domains={dom.domains}"
                    + (f" backtrack={len(backtracking)}" if backtracking else "")
                )

    # ------------------------------------------------------------------ helpers
    def _taric_targets_for_candidate(self, cand: dict) -> list[dict[str, Any]]:
        branches = cand.get("taric10_branch_candidates") or []
        targets: list[dict[str, Any]] = []
        seen: set[str] = set()
        if branches:
            has_declarable_flag = any(
                isinstance(branch, dict) and "is_declarable_leaf" in branch
                for branch in branches
            )
            skipped_non_declarable = 0
            for branch in branches:
                taric10 = _compact_text(branch.get("taric10"))
                if not taric10 or taric10.startswith("99999999") or taric10 in seen:
                    continue
                if has_declarable_flag and not _truthy(branch.get("is_declarable_leaf")):
                    skipped_non_declarable += 1
                    continue
                seen.add(taric10)
                targets.append({
                    "taric10": taric10,
                    "branch": dict(branch),
                    "resolution_mode": cand.get("taric10_resolution_mode") or "enumerate_all_under_cn8",
                })
            if skipped_non_declarable:
                self.reason(
                    f"Skipped {skipped_non_declarable} non-declarable TARIC branch(es) "
                    f"for candidate {cand.get('candidate_id')} CN8={cand.get('cn8')}."
                )
        else:
            taric10 = _compact_text(cand.get("taric10"))
            if taric10 and not taric10.startswith("99999999"):
                targets.append({
                    "taric10": taric10,
                    "branch": None,
                    "resolution_mode": cand.get("taric10_resolution_mode") or "single_taric10",
                })

        count = len(targets)
        for index, target in enumerate(targets, start=1):
            target["branch_index"] = index
            target["branch_count"] = count
        return targets

    def _domain_result_from_routing_context(self, routing_context: dict, cn8: str):
        if not routing_context:
            return None
        domains = list(routing_context.get("domain_scopes") or [])
        pre_gate_domains = list(routing_context.get("pre_gate_domains") or [])
        if not domains and not pre_gate_domains:
            return None
        candidates = routing_context.get("candidate_chapters") or []
        chapter = cn8[:2]
        matched = next(
            (
                c for c in candidates
                if str(c.get("chapter") or "").zfill(2) == chapter
            ),
            candidates[0] if candidates else {},
        )
        confidence = matched.get("confidence")
        if confidence is None:
            confidence = routing_context.get("confidence", 0.8)
        return SimpleNamespace(
            domains=domains or ["other"],
            pre_gate_domains=pre_gate_domains,
            chapter=chapter,
            confidence=confidence,
            decided_by="routing_context",
            reason="Reused upstream RoutingContext for DocumentAgent scope.",
            missing_facts=list(routing_context.get("missing_facts") or []),
            evidence=list(routing_context.get("evidence") or []),
            is_ambiguous=bool(routing_context.get("conflicts")),
        )

    def _split_requirements_for_view(
        self,
        raw: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        reqs = raw.get("requirements") or []
        kr = [r for r in reqs if r.get("applies_to_korea")]
        non_kr = [r for r in reqs if not r.get("applies_to_korea")]
        controls: list[dict[str, Any]] = []
        duties: list[dict[str, Any]] = []
        for req in kr:
            if req.get("measure_type") == "Product regulatory requirements":
                continue
            if _view_is_control_measure(req):
                controls.append(req)
            elif _view_is_duty_measure(req):
                duties.append(req)
            else:
                duties.append(req)
        return kr, non_kr, controls, duties

    def _build_document_view(
        self,
        *,
        raw: dict[str, Any],
        dom,
        customs: list[dict],
        duties: list[dict],
        preferential: list[dict],
        required_documents: list[dict],
        product_regulations: list[dict],
        basic_duty: dict,
    ) -> dict[str, Any]:
        """Render-independent document package view model.

        This mirrors the newer Dash bucketing logic, but keeps it in the
        Document_Agent so the UI can eventually become display-only.
        """
        kr, non_kr, view_controls, view_duties = self._split_requirements_for_view(raw)
        baseline_reqs = [
            r for r in kr
            if r.get("measure_type") == "Baseline document requirements"
        ]
        baseline_details = [
            detail
            for req in baseline_reqs
            for detail in (req.get("detailed_requirements") or [])
        ]
        product_reqs = [
            r for r in kr
            if r.get("measure_type") in (
                "Product regulatory requirements",
                "Pre-TARIC screening requirements",
            )
        ]
        taric_triggered_details = [
            detail
            for req in kr
            for detail in (req.get("detailed_requirements") or [])
            if detail.get("source_layer") == "taric_triggered"
        ]
        product_details = [
            detail
            for req in product_reqs
            for detail in (req.get("detailed_requirements") or [])
        ]
        pre_details = [
            d for d in product_details
            if d.get("source_layer") in ("pre_taric_gate", "chapter_route_seed")
        ]
        post_details = [d for d in product_details if d.get("source_layer") == "product_domain_seed"]
        related_declarations = _related_declarations_by_domain(product_reqs, kr)

        third_country = _view_find_measure(view_duties, ("Third country duty",))
        fta_pref = _view_find_measure(view_duties, ("Tariff preference", "Customs Union"))
        additional_duty = _view_find_measure(view_duties, ("Additional duties",))
        base_duty_measures = [r for r in view_duties if _view_is_base_duty_measure(r)]
        preferential_measures = [r for r in view_duties if _view_is_preferential_measure(r)]

        checklist = raw.get("checklist_summary") or {}
        binding_documents = checklist.get("document_binding_cards") or []
        document_groups = checklist.get("document_groups") or []
        missing = raw.get("missing_facts") or checklist.get("missing_facts") or []
        total_measure_rows = int(raw.get("total_measure_rows") or 0)
        if total_measure_rows <= 0:
            post_taric_status = "no_taric_measure_rows"
            post_taric_message = (
                "No current TARIC measure rows were found for this TARIC10. "
                "Baseline and pre-TARIC checks are still shown when routed."
            )
        elif not taric_triggered_details:
            post_taric_status = "no_post_taric_requirement_match"
            post_taric_message = (
                "No curated post-TARIC document requirement matched the current "
                "measure/certificate/legal-base context. Baseline and pre-TARIC "
                "documents may still be required."
            )
        else:
            post_taric_status = "post_taric_requirements_found"
            post_taric_message = "Post-TARIC requirement rows matched this TARIC context."

        return {
            "source": "DocumentAgent.document_view.v1",
            "taric10": raw.get("taric10"),
            "cn8": raw.get("cn8"),
            "total_measure_rows": total_measure_rows,
            "post_taric_status": post_taric_status,
            "post_taric_message": post_taric_message,
            "domains": list(getattr(dom, "domains", []) or []),
            "domain_confidence": getattr(dom, "confidence", None),
            "metrics": {
                "kr_measure_count": len(kr),
                "non_kr_measure_count": len(non_kr),
                "control_count": len(view_controls),
                "duty_count": len(view_duties),
                "base_duty_count": len(base_duty_measures),
                "preferential_count": len(preferential_measures),
                "document_group_count": len(document_groups),
                "required_document_count": len(required_documents),
                "baseline_document_count": len(binding_documents) or len(baseline_details),
                "product_rule_count": len(product_details),
                "product_pre_count": len(pre_details),
                "product_post_count": len(post_details),
                "taric_triggered_post_count": len(taric_triggered_details),
                "missing_count": len(missing),
                "document_binding_count": len(binding_documents),
            },
            "sections": {
                "overview": {
                    "counts": checklist.get("counts") or {},
                    "missing_facts": missing,
                    "third_country_duty": third_country,
                    "fta_preference": fta_pref,
                    "additional_duty": additional_duty,
                    "basic_duty": basic_duty,
                },
                "customs_check_items": {
                    "agent_bucket": customs,
                    "render_bucket": view_controls,
                },
                "basic_duty": {
                    "agent_bucket": duties,
                    "render_bucket": base_duty_measures,
                    "selected": basic_duty,
                },
                "preferential_evidence": {
                    "agent_bucket": preferential,
                    "render_bucket": preferential_measures,
                },
                "required_documents": {
                    "agent_bucket": required_documents,
                    "document_groups": document_groups,
                },
                "pre_taric_checks": {
                    "checks": pre_details,
                    "source": "pre_taric_requirement_master",
                },
                "post_taric_requirements": {
                    "status": post_taric_status,
                    "message": post_taric_message,
                    "details": taric_triggered_details,
                    "source": "post_taric_requirement_master",
                },
                "baseline_documents": {
                    "requirements": baseline_reqs,
                    "documents": binding_documents or [_enrich_baseline_doc(d) for d in baseline_details],
                    "source": "document_binding" if binding_documents else "baseline_document_master",
                },
                "product_regulations": {
                    "agent_bucket": product_regulations,
                    "requirements": product_reqs,
                    "pre": pre_details,
                    "post": post_details,
                    "related_declarations": related_declarations,
                },
            },
        }

    def _bucket_requirements(self, raw_requirements: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
        customs, duties, preferential = [], [], []
        for req in raw_requirements:
            mt = req.get("measure_type") or ""
            bucket = _classify_measure(mt)
            item = {
                "measure_type": mt,
                "applies_to_korea": req.get("applies_to_korea", False),
                "origins": req.get("origins") or [],
                "duty": req.get("duty") or {},
                "legal_base": req.get("legal_base"),
                "celex_id": (req.get("celex") or {}).get("celex_id") if isinstance(req.get("celex"), dict) else None,
                "cert_codes": [c.get("code") for c in (req.get("certificates") or [])],
            }
            if bucket == "customs_check":
                customs.append(item)
            elif bucket == "basic_duty":
                duties.append(item)
            elif bucket == "preferential":
                preferential.append(item)
            else:
                # 'other' bucket goes into customs_check by default (catch-all)
                customs.append(item)
        return customs, duties, preferential

    def _extract_required_documents(self, raw_requirements: list[dict]) -> list[dict]:
        seen: dict[str, dict] = {}
        for req in raw_requirements:
            for c in req.get("certificates") or []:
                code = (c.get("code") or "").strip()
                if not code:
                    continue
                kind = _cert_kind(code)
                # exemption_declaration / preferential_origin 는 별도 section
                if kind in ("exemption_declaration", "preferential_origin"):
                    continue
                if code in seen:
                    continue
                seen[code] = {
                    "doc_kind": kind,
                    "code": code,
                    "title": c.get("description") or code,
                    "required_when": req.get("measure_type") or "",
                    "celex_id": (req.get("celex") or {}).get("celex_id") if isinstance(req.get("celex"), dict) else None,
                }
        return list(seen.values())

    def _build_product_regulations(self, dom) -> list[dict]:
        out: list[dict] = []
        # Map each domain to its rule family + scope (rough MVP — real CELEX
        # binding 후속 step 에서).
        family_map = {
            "food":            {"family": "EU 2017/625 + 178/2002", "scope": "human consumption food"},
            "animal_origin":   {"family": "EU 853/2004 + CHED-P (Reg 2021/632)", "scope": "products of animal origin"},
            "cosmetics":       {"family": "EU 1223/2009 (Cosmetics Regulation)", "scope": "cosmetic products"},
            "cites":           {"family": "EU 338/97 (CITES implementation)", "scope": "CITES-listed species"},
            "hazardous":       {"family": "EU REACH 1907/2006 + CLP 1272/2008", "scope": "chemical substances"},
            "pharmaceutical":  {"family": "EU 2001/83 (medicinal products)", "scope": "human/veterinary medicines"},
            "sanctions":       {"family": "EU sanctions regulations (varies)", "scope": "restricted destinations/items"},
            "dual_use":        {"family": "EU 2021/821 (dual-use)", "scope": "dual-use items"},
            "other":           {"family": "n/a", "scope": "general goods"},
            "unknown":         {"family": "n/a", "scope": "unknown"},
        }
        for d in dom.domains:
            meta = family_map.get(d, family_map["other"])
            out.append({
                "domain": d,
                "rule_family": meta["family"],
                "scope": meta["scope"],
                "applies": "applies" if dom.confidence >= 0.7 else "possibly_applies",
                "missing_facts": list(dom.missing_facts),
            })
        return out

    def _collect_celex(self, raw_requirements: list[dict]) -> list[dict]:
        seen: dict[str, dict] = {}
        for req in raw_requirements:
            celex = req.get("celex") or {}
            if not isinstance(celex, dict):
                continue
            cid = (celex.get("celex_id") or "").strip()
            if cid and cid not in seen:
                seen[cid] = {
                    "celex_id": cid,
                    "title": celex.get("title") or "",
                    "match_status": celex.get("match_status") or "matched_ok",
                    "for_measure_type": req.get("measure_type"),
                }
        return list(seen.values())

    def _suggest_external(self, dom) -> list[dict]:
        out: list[dict] = []
        if "food" in dom.domains:
            out.append({"name": "EU TRACES system", "url": "https://traces.ec.europa.eu/"})
        if "animal_origin" in dom.domains:
            out.append({"name": "EU approved establishment list",
                        "url": "https://food.ec.europa.eu/safety/biological-safety/food-hygiene/establishments_en"})
        if "cosmetics" in dom.domains:
            out.append({"name": "CPNP (Cosmetic Products Notification Portal)",
                        "url": "https://ec.europa.eu/growth/sectors/cosmetics/cpnp_en"})
        if "cites" in dom.domains:
            out.append({"name": "CITES species database",
                        "url": "https://speciesplus.net/"})
        return out

    def _pick_basic_duty(self, duties: list[dict], raw: dict) -> dict:
        # Prefer 'Third country duty' as the canonical basic_duty.
        for d in duties:
            if "Third country duty" in (d.get("measure_type") or ""):
                return {
                    "rate": (d.get("duty") or {}).get("rate") or (d.get("duty") or {}).get("text"),
                    "measure_type": d["measure_type"],
                    "legal_base": d.get("legal_base"),
                    "celex_id": d.get("celex_id"),
                }
        if duties:
            d = duties[0]
            return {
                "rate": (d.get("duty") or {}).get("rate") or (d.get("duty") or {}).get("text"),
                "measure_type": d["measure_type"],
                "legal_base": d.get("legal_base"),
                "celex_id": d.get("celex_id"),
            }
        return {}

    def _backtracking_signals(self, cand: dict, raw: dict, dom) -> list[dict]:
        signals: list[dict] = []
        # 1. No measure rows → 분류 잘못 가능
        if not raw.get("has_data"):
            signals.append({
                "type": "no_measures_for_taric10",
                "candidate_id": cand["candidate_id"],
                "reason": (
                    f"TARIC10 {cand.get('taric10')} returned no current measure rows. "
                    "Verify the selected TARIC branch is the right leaf."
                ),
            })
        # 2. 도메인 ambiguous → 분류에 영향
        if dom.is_ambiguous:
            signals.append({
                "type": "domain_ambiguous",
                "candidate_id": cand["candidate_id"],
                "reason": (
                    f"Regulatory domain unresolved (domains={dom.domains}); ingredient "
                    "ratio likely required."
                ),
                "missing_facts": list(dom.missing_facts),
            })
        return signals

    def _emit_unresolved_package(
        self,
        store: BlackboardStore,
        cand: dict,
        *,
        reason: str = "candidate_unresolved",
    ) -> None:
        """For needs_more_facts / 99999999 candidates, emit a stub package with
        only the backtracking signal — no measure pretense."""
        dp_id = store.next_id("dp")
        store.append("document_packages", {
            "object_type": "DocumentPackage",
            "created_by": self.agent_name,
            "created_at": now_iso(),
            "document_package_id": dp_id,
            "candidate_id": cand["candidate_id"],
            "cn8": cand.get("cn8"),
            "taric10": cand.get("taric10"),
            "customs_check_items": [],
            "basic_duty": {},
            "preferential_evidence": [],
            "required_documents": [],
            "product_regulations": [],
            "document_view": {
                "source": "DocumentAgent.document_view.v1",
                "taric10": cand.get("taric10"),
                "cn8": cand.get("cn8"),
                "metrics": {
                    "kr_measure_count": 0,
                    "control_count": 0,
                    "duty_count": 0,
                    "document_group_count": 0,
                    "product_rule_count": 0,
                    "missing_count": 1,
                },
                "sections": {},
            },
            "missing_facts": ["candidate_unresolved"],
            "external_lookup": [],
            "celex_basis": [],
            "backtracking_signals": [{
                "type": "candidate_not_classified",
                "candidate_id": cand["candidate_id"],
                "reason": reason,
            }],
            "summary": {
                "duty": "unknown",
                "main_requirements": [],
                "domains": ["unknown"],
                "unknowns": ["candidate_unresolved"],
            },
            "conflicts": [],
        })
        self.wrote(dp_id)
        self.reason(
            f"Empty package for unresolved cand {cand['candidate_id']} ({reason}); "
            "emitted backtracking signal."
        )

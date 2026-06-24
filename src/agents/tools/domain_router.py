"""
DomainRouterTool — ProductUnderstanding/Classification 전 chapter/domain route.

Owned by the pre-classification routing stage and reused by Document_Agent.
Hybrid design:

  1) Deterministic fast-path  ← THIS STEP
     - cn_chapter_index + domain_scope_routes
     - chapter → 1-2 도메인 매핑 테이블
     - chapter note decision axes / prepared-food guardrails
     - 명백한 키워드 (예: "lipstick" → cosmetics)
     - 단일 도메인 결과면 그대로 반환

  2) LLM fallback (낙지볶음 같은 ambiguous case)
     - 추후 step 에서 wire (bridge.RuntimeAdapter 재사용)
     - 현재는 ambiguous 표시만 반환 → Document_Agent 가 backtracking_signal 발행

도메인 vocabulary (asap_evidence.yaml 의 RegulatoryDomain enum 과 일치):
  food / cosmetics / animal_origin / cites / hazardous /
  pharmaceutical / sanctions / dual_use / other
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from agents import document_package as _db


# ---------------------------------------------------------------------------
# Fast-path table — chapter → primary regulatory domains.
# 한 chapter 에 여러 domain 매핑 가능 (예: 02류 = food + animal_origin).
# 출처: Domain_Scope_Routes spec (v2 core) + EU 일반 규제 매트릭스.
# ---------------------------------------------------------------------------
_CHAPTER_TO_DOMAINS: dict[str, list[str]] = {
    # Live animals + animal products (SPS, CHED-P 영역)
    "01": ["animal_origin"],                       # live animals
    "02": ["food", "animal_origin"],               # meat
    "03": ["food", "animal_origin"],               # fish/seafood
    "04": ["food", "animal_origin"],               # dairy/eggs/honey
    "05": ["animal_origin"],                       # products of animal origin n.e.s.
    # Plant products (SPS 식물 검역)
    "06": ["food"],                                # live plants/flowers (선택적 SPS)
    "07": ["food"],                                # vegetables
    "08": ["food"],                                # fruits
    "09": ["food"],                                # coffee/tea/spices
    "10": ["food"],                                # cereals
    "11": ["food"],                                # flour/starch
    "12": ["food"],                                # oilseeds
    "13": ["food"],                                # vegetable saps/extracts
    "14": ["food"],                                # vegetable plaiting materials
    "15": ["food"],                                # animal/vegetable fats and oils
    # Prepared food (가공식품 — Phase 1 핵심)
    "16": ["food", "animal_origin"],               # meat/fish preparations
    "17": ["food"],                                # sugars/sugar confectionery
    "18": ["food"],                                # cocoa/chocolate
    "19": ["food"],                                # cereal preparations (라면 등)
    "20": ["food"],                                # preparations of vegetables/fruits
    "21": ["food"],                                # misc edible preparations (소스 등)
    "22": ["food"],                                # beverages
    "23": ["food", "animal_origin"],               # animal feed
    "24": ["food"],                                # tobacco
    # Mineral / chemical (REACH/CMR 영역)
    "25": ["hazardous"],                           # salt/cement/sulphur
    "26": ["hazardous"],                           # ores
    "27": ["hazardous"],                           # mineral fuels
    "28": ["hazardous"],                           # inorganic chemicals
    "29": ["hazardous", "pharmaceutical"],         # organic chemicals (의약품 원료 포함)
    "30": ["pharmaceutical"],                      # pharmaceutical products
    "31": ["hazardous"],                           # fertilizers
    "32": ["hazardous", "cosmetics"],              # tanning/dyeing extracts
    "33": ["cosmetics"],                           # essential oils/cosmetics
    "34": ["cosmetics", "hazardous"],              # soap/wax (계면활성제)
    "35": ["hazardous"],                           # albuminoidal substances
    "36": ["hazardous"],                           # explosives/pyrotechnics
    "37": ["hazardous"],                           # photographic chemicals
    "38": ["hazardous"],                           # misc chemicals
    # Plastics/rubber/leather/wood — 일반 (보통 도메인 없음)
    # Textiles/footwear — 일반 (CMR substance 시 hazardous)
    # Metals/machinery/vehicles — 대부분 dual_use 후보
    "84": ["dual_use"],                            # nuclear reactors/machinery (선택적)
    "85": ["dual_use"],                            # electrical machinery
    "87": ["dual_use"],                            # vehicles (군용 후보)
    "93": ["dual_use", "sanctions"],               # arms/ammunition
}


# CITES 후보 chapter (특정 동식물 protected) — 추가 트리거
_CITES_CHAPTER_HINTS: set[str] = {"01", "02", "03", "05", "06", "12", "13", "33"}


# 명백한 키워드 → 도메인 (product_name 에 등장 시)
_KEYWORD_TO_DOMAIN: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(r"\b(lipstick|mascara|foundation|perfume|cologne|cream|lotion|cosmetic|shampoo|skincare|toner|essence)\b", re.I),
     ["cosmetics"]),
    (re.compile(r"\b(립스틱|마스카라|파운데이션|향수|크림|로션|화장품|샴푸|스킨|토너|에센스)\b"),
     ["cosmetics"]),
    (re.compile(r"\b(pharmaceutical|medicine|drug|tablet|capsule|antibiotic|vaccine)\b", re.I),
     ["pharmaceutical"]),
    (re.compile(r"\b(의약품|약품|항생제|백신|치료제)\b"),
     ["pharmaceutical"]),
    (re.compile(r"\b(weapon|firearm|ammunition|missile|explosive)\b", re.I),
     ["sanctions", "dual_use"]),
]


# Generic product-form anchors used only for chapter routing. These are not a
# final CN rule; they give DomainRouter useful top-chapter hints when official
# chapter keywords are English-only but evidence is Korean OCR text.
_PRODUCT_FORM_TO_CHAPTER: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\b(noodle|ramen|pasta|macaroni|spaghetti)\b|라면|유탕면|국수|면류|파스타"), "19", "cereal/noodle preparation"),
    (re.compile(r"\b(sauce|seasoning|condiment|soup|broth|stock)\b|소스|양념|조미|스프|국|탕|찌개|육수"), "21", "miscellaneous edible preparation"),
    (re.compile(r"\b(dumpling|mandu|sausage|ham|surimi)\b|만두|소시지|햄|어묵|맛살|멘보샤"), "16", "meat/fish/crustacean preparation"),
    (re.compile(r"\b(jam|pickle|fruit preparation|vegetable preparation)\b|잼|절임|피클|과실가공|채소가공"), "20", "vegetable/fruit preparation"),
    (re.compile(r"\b(beverage|drink|juice|tea)\b|음료|주스|차음료"), "22", "beverage"),
]


@dataclass
class DomainRouteResult:
    """One product → one or more applicable regulatory domains."""
    domains: list[str] = field(default_factory=list)            # ["food", "animal_origin"]
    pre_gate_domains: list[str] = field(default_factory=list)   # ["sanctions", "cites"]
    chapter: str = ""
    confidence: float = 0.0
    decided_by: str = ""                                        # "fast_path_chapter" | "fast_path_keyword" | "ambiguous" | "llm"
    reason: str = ""
    missing_facts: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    is_ambiguous: bool = False                                  # True → caller 가 LLM fallback 호출

    def to_dict(self) -> dict:
        return {
            "domains": list(self.domains),
            "pre_gate_domains": list(self.pre_gate_domains),
            "chapter": self.chapter,
            "confidence": self.confidence,
            "decided_by": self.decided_by,
            "reason": self.reason,
            "missing_facts": list(self.missing_facts),
            "evidence": list(self.evidence),
            "is_ambiguous": self.is_ambiguous,
        }


class DomainRouterTool:
    """fast-path + (planned) LLM hybrid. Currently fast-path only.

    Usage:
        tool = DomainRouterTool()
        result = tool.route(
            cn8="19023010",
            product_facts={"product_name": "Korean instant ramen noodle"},
            measure_type_hints=["Veterinary control"],
        )
    """

    def __init__(self, llm_adapter=None) -> None:
        self._llm_adapter = llm_adapter

    @staticmethod
    def _split_values(value: str) -> list[str]:
        seen: list[str] = []
        for item in str(value or "").replace("|", ";").replace(",", ";").split(";"):
            item = item.strip()
            if item and item not in seen:
                seen.append(item)
        return seen

    def _fetch_chapter_row(self, chapter: str) -> dict:
        conn = None
        try:
            conn = _db._connect_db()
            cur = conn.cursor()
            if not _db._table_exists(cur, "cn_chapter_index"):
                cur.close()
                return {}
            cur.execute("SELECT * FROM cn_chapter_index WHERE chapter = %s LIMIT 1", (chapter,))
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            cur.close()
            return dict(zip(cols, row)) if row else {}
        except Exception:
            return {}
        finally:
            _db._release_db(conn)

    def _fetch_all_chapter_rows(self) -> list[dict]:
        conn = None
        try:
            conn = _db._connect_db()
            cur = conn.cursor()
            if not _db._table_exists(cur, "cn_chapter_index"):
                cur.close()
                return []
            cur.execute("SELECT * FROM cn_chapter_index ORDER BY chapter")
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            cur.close()
            return rows
        except Exception:
            return []
        finally:
            _db._release_db(conn)

    def _fetch_domain_routes(self, chapter: str) -> list[dict]:
        conn = None
        try:
            conn = _db._connect_db()
            cur = conn.cursor()
            if not _db._table_exists(cur, "domain_scope_routes"):
                cur.close()
                return []
            cur.execute(
                """
                SELECT *
                FROM domain_scope_routes
                WHERE chapters = 'ALL'
                   OR %s = ANY(string_to_array(replace(coalesce(chapters, ''), ' ', ''), ';'))
                ORDER BY domain_scope
                """,
                (chapter,),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            cur.close()
            return rows
        except Exception:
            return []
        finally:
            _db._release_db(conn)

    def _domains_from_routes(self, chapter: str, chapter_row: dict | None = None) -> tuple[list[str], list[str], list[dict]]:
        domains: list[str] = []
        pre_gate_domains: list[str] = []
        evidence: list[dict] = []

        pre_gate_names = {"sanctions", "cites"}
        for row in self._fetch_domain_routes(chapter):
            scope = (row.get("domain_scope") or "").strip()
            if scope in pre_gate_names:
                pre_gate_domains.append(scope)
            else:
                domains.append(scope)
            evidence.append({
                "source": "domain_scope_routes",
                "domain_scope": scope,
                "chapter": chapter,
                "chapters": row.get("chapters") or "",
                "notes": row.get("notes") or "",
                })

        if not domains:
            domains = list(_CHAPTER_TO_DOMAINS.get(chapter, []))
            if domains:
                evidence.append({
                    "source": "fallback_hardcoded_chapter",
                    "chapter": chapter,
                    "domains": domains,
                })

        if not pre_gate_domains:
            chapter_row = chapter_row or {}
            pre_gate_domains = self._split_values(chapter_row.get("pre_gate_domain_candidates") or "")
            if pre_gate_domains:
                evidence.append({
                    "source": "cn_chapter_index",
                    "chapter": chapter,
                    "pre_gate_domains": pre_gate_domains,
                })
        if not pre_gate_domains:
            pre_gate_domains = ["sanctions"]
            if chapter in _CITES_CHAPTER_HINTS:
                pre_gate_domains.append("cites")
            evidence.append({
                "source": "fallback_pre_gate",
                "chapter": chapter,
                "pre_gate_domains": pre_gate_domains,
            })

        return domains, pre_gate_domains, evidence

    @staticmethod
    def _term_matches(terms: list[str], haystack: str) -> list[str]:
        out: list[str] = []
        normalized = f" {re.sub(r'\\s+', ' ', haystack or '').lower()} "
        for term in terms:
            term_norm = re.sub(r"\s+", " ", str(term or "").strip().lower())
            if len(term_norm) < 2:
                continue
            if term_norm in normalized and term not in out:
                out.append(term)
        return out

    def route_product(
        self,
        *,
        product_understanding: dict[str, Any],
        product_facts: dict[str, Any] | None = None,
        top_k: int = 5,
    ) -> dict[str, Any]:
        """Route product evidence to chapter/domain candidates before CN8.

        This is a soft-routing read model for Classification_Agent. It does
        not eliminate chapters; it provides top chapter candidates and
        guardrail evidence, especially raw-vs-prepared food redirects.
        """
        product_facts = product_facts or {}
        text_parts = [
            product_understanding.get("classification_text") or "",
            " ".join(product_understanding.get("keywords") or []),
            " ".join(product_understanding.get("routing_terms") or []),
            product_facts.get("product_name") or "",
            product_facts.get("description") or "",
        ]
        haystack = "\n".join(str(p) for p in text_parts if p)
        processing_state = product_understanding.get("processing_state") or "unknown"
        processed = processing_state == "processed_or_prepared" or bool(
            product_understanding.get("processing_signals")
        )
        domain_hints = set(product_understanding.get("domain_hints") or [])

        rows = self._fetch_all_chapter_rows()
        row_by_chapter = {str(r.get("chapter") or "").zfill(2): r for r in rows}
        scores: dict[str, dict[str, Any]] = {}
        blocked: list[dict[str, Any]] = []
        redirect_bonus: dict[str, float] = {}

        for row in rows:
            chapter = str(row.get("chapter") or "").zfill(2)
            if not chapter:
                continue
            keyword_terms = self._split_values(row.get("chapter_keywords") or "")
            prepared_terms = self._split_values(row.get("prepared_scope_signals") or "")
            raw_terms = self._split_values(row.get("raw_scope_signals") or "")
            domain_terms = self._split_values(row.get("domain_scope_candidates") or "")

            keyword_matches = self._term_matches(keyword_terms, haystack)
            prepared_matches = self._term_matches(prepared_terms, haystack)
            raw_matches = self._term_matches(raw_terms, haystack)
            score = float(len(keyword_matches) * 4)
            if processed:
                score += len(prepared_matches) * 5
                if raw_matches:
                    score -= 2
            else:
                score += len(raw_matches) * 3

            form_matches: list[str] = []
            for pattern, target_chapter, reason in _PRODUCT_FORM_TO_CHAPTER:
                m = pattern.search(haystack)
                if m and target_chapter == chapter:
                    score += 8.0
                    form_matches.append(f"{m.group(0)}:{reason}")

            redirects = self._split_values(row.get("prepared_food_redirect_chapters") or "")
            if processed and raw_matches and redirects:
                blocked.append({
                    "chapter": chapter,
                    "chapter_title": row.get("chapter_title") or "",
                    "reason": "processed_product_guardrail_redirect",
                    "matched_raw_terms": raw_matches,
                    "redirect_chapters": redirects,
                })
                for redirect in redirects:
                    redirect_chapter = re.sub(r"\D", "", redirect)[:2].zfill(2)
                    if redirect_chapter:
                        redirect_bonus[redirect_chapter] = redirect_bonus.get(redirect_chapter, 0.0) + 5.0

            if score <= 0:
                continue
            scores[chapter] = {
                "chapter": chapter,
                "chapter_title": row.get("chapter_title") or "",
                "score": score,
                "matched_terms": keyword_matches + prepared_matches + raw_matches + form_matches,
                "form_matches": form_matches,
                "keyword_matches": keyword_matches,
                "prepared_matches": prepared_matches,
                "raw_matches": raw_matches,
                "routing_summary": row.get("routing_summary") or "",
                "routing_guardrails": row.get("routing_guardrails") or "",
                "classification_decision_axes": self._split_values(
                    row.get("classification_decision_axes") or ""
                ),
                "prepared_food_redirect_chapters": redirects,
            }

        for chapter, bonus in redirect_bonus.items():
            row = row_by_chapter.get(chapter, {})
            scores.setdefault(chapter, {
                "chapter": chapter,
                "chapter_title": row.get("chapter_title") or "",
                "score": 0.0,
                "matched_terms": [],
                "keyword_matches": [],
                "prepared_matches": [],
                "raw_matches": [],
                "routing_summary": row.get("routing_summary") or "",
                "routing_guardrails": row.get("routing_guardrails") or "",
                "classification_decision_axes": self._split_values(
                    row.get("classification_decision_axes") or ""
                ),
                "prepared_food_redirect_chapters": [],
            })
            scores[chapter]["score"] += bonus
            scores[chapter].setdefault("routing_adjustments", []).append({
                "type": "prepared_food_redirect_bonus",
                "bonus": bonus,
            })

        if not scores and "food" in domain_hints:
            for chapter in ("16", "19", "20", "21", "22"):
                row = row_by_chapter.get(chapter, {})
                scores[chapter] = {
                    "chapter": chapter,
                    "chapter_title": row.get("chapter_title") or "",
                    "score": 1.0,
                    "matched_terms": ["food_domain_hint"],
                    "keyword_matches": [],
                    "prepared_matches": [],
                    "raw_matches": [],
                    "routing_summary": row.get("routing_summary") or "",
                    "routing_guardrails": row.get("routing_guardrails") or "",
                    "classification_decision_axes": self._split_values(
                        row.get("classification_decision_axes") or ""
                    ),
                    "prepared_food_redirect_chapters": [],
                }

        ranked = sorted(scores.values(), key=lambda x: (-float(x.get("score") or 0), x.get("chapter") or ""))
        max_score = float(ranked[0]["score"]) if ranked else 0.0
        candidates: list[dict[str, Any]] = []
        domain_scopes: list[str] = []
        pre_gate_domains: list[str] = []
        evidence: list[dict[str, Any]] = []
        for item in ranked[: max(1, top_k)]:
            chapter = item["chapter"]
            row = row_by_chapter.get(chapter, {})
            domains, pre_gates, route_evidence = self._domains_from_routes(chapter, row)
            for d in domains:
                if d not in domain_scopes:
                    domain_scopes.append(d)
            for d in pre_gates:
                if d not in pre_gate_domains:
                    pre_gate_domains.append(d)
            evidence.extend(route_evidence)
            item = dict(item)
            item["confidence"] = round(min(0.95, max(0.2, item["score"] / (max_score + 1.0))), 3) if max_score else 0.2
            item["domain_scopes"] = domains
            item["pre_gate_domains"] = pre_gates
            candidates.append(item)

        return {
            "candidate_chapters": candidates,
            "blocked_chapters": blocked,
            "domain_scopes": domain_scopes,
            "pre_gate_domains": pre_gate_domains,
            "processing_state": processing_state,
            "soft_filter": True,
            "routing_basis": {
                "method": "cn_chapter_index_keyword_guardrail",
                "table": "cn_chapter_index",
                "top_k": top_k,
                "processed_guardrail_applied": bool(blocked),
            },
            "missing_facts": [
                "primary_ingredient_ratio"
                for _ in [0]
                if not re.search(r"\b\d{1,3}\s*%", haystack)
            ],
            "evidence": evidence,
        }

    def route(
        self,
        *,
        cn8: str,
        product_facts: dict,
        measure_type_hints: list[str] | None = None,
    ) -> DomainRouteResult:
        cn8 = (cn8 or "").strip()
        chapter = cn8[:2] if len(cn8) >= 2 else ""
        name = (product_facts.get("product_name") or "").strip()
        desc = (product_facts.get("description") or "").strip()
        haystack = f"{name} {desc}"

        evidence: list[dict] = []
        domains: list[str] = []
        pre_gate_domains: list[str] = []

        # 1. Chapter fast-path
        chapter_row = self._fetch_chapter_row(chapter)
        chapter_domains, route_pre_gates, route_evidence = self._domains_from_routes(chapter, chapter_row)
        if chapter_domains:
            domains = list(chapter_domains)
        if route_pre_gates:
            pre_gate_domains = list(route_pre_gates)
        evidence.extend(route_evidence)
        if chapter_row:
            evidence.append({
                "source": "cn_chapter_index",
                "chapter": chapter,
                "chapter_title": chapter_row.get("chapter_title") or "",
                "routing_summary": chapter_row.get("routing_summary") or "",
                "classification_decision_axes": self._split_values(chapter_row.get("classification_decision_axes") or ""),
                "prepared_food_redirect_chapters": self._split_values(chapter_row.get("prepared_food_redirect_chapters") or ""),
                "explicit_exclusion_refs": self._split_values(chapter_row.get("explicit_exclusion_refs") or ""),
                "routing_guardrails": chapter_row.get("routing_guardrails") or "",
                "source_note_coverage": chapter_row.get("source_note_coverage") or "",
            })

        # 2. Keyword override / extension
        for pat, kw_domains in _KEYWORD_TO_DOMAIN:
            if pat.search(haystack):
                for d in kw_domains:
                    if d not in domains:
                        domains.append(d)
                evidence.append({
                    "source": "fast_path_keyword",
                    "pattern": pat.pattern,
                    "domains": kw_domains,
                })

        # 3. CITES trigger from measure hints (Veterinary / CITES measures already
        # show CITES in TARIC. Mirror that as a domain.)
        hints = [m.lower() for m in (measure_type_hints or [])]
        if any("cites" in h for h in hints):
            if "cites" not in domains:
                domains.append("cites")
            if "cites" not in pre_gate_domains:
                pre_gate_domains.append("cites")
            evidence.append({"source": "measure_hint", "hint": "CITES"})
        if any("veterinary" in h for h in hints):
            if "animal_origin" not in domains:
                domains.append("animal_origin")
            evidence.append({"source": "measure_hint", "hint": "Veterinary control"})
        if chapter in _CITES_CHAPTER_HINTS:
            # Mark CITES as a *candidate* only; do not auto-promote without a
            # measure hint or species name confirmation.
            pass

        # 4. Decision
        if not domains:
            return DomainRouteResult(
                domains=["other"],
                pre_gate_domains=pre_gate_domains,
                chapter=chapter,
                confidence=0.2,
                decided_by="fast_path_unmatched",
                reason=f"No fast-path rule matched chapter={chapter!r}.",
                missing_facts=["domain_unknown"],
                evidence=evidence,
                is_ambiguous=True,
            )

        # Ambiguity heuristic: >2 domains OR (food + animal_origin + product description has
        # 함량/비율 unclear) → LLM should weigh ingredient ratios.
        is_ambiguous = len(domains) >= 3 or (
            "food" in domains and "animal_origin" in domains
            and not re.search(r"\b\d{1,3}\s*%", haystack)
        )

        decided_by = "fast_path_chapter"
        confidence = 0.8 if not is_ambiguous else 0.5
        reason = (
            f"chapter {chapter} → {domains}"
            + (f"; keyword+={[p.pattern for p,_ in _KEYWORD_TO_DOMAIN if p.search(haystack)]}"
               if any(p.search(haystack) for p,_ in _KEYWORD_TO_DOMAIN) else "")
        )
        if pre_gate_domains:
            reason += f"; pre_gate={pre_gate_domains}"
        missing: list[str] = []
        if is_ambiguous:
            missing = ["primary_ingredient_ratio", "animal_origin_content_pct"]
            reason += " — multi-domain detected; recommend LLM weighting."

        return DomainRouteResult(
            domains=domains,
            pre_gate_domains=pre_gate_domains,
            chapter=chapter,
            confidence=confidence,
            decided_by=decided_by,
            reason=reason,
            missing_facts=missing,
            evidence=evidence,
            is_ambiguous=is_ambiguous,
        )

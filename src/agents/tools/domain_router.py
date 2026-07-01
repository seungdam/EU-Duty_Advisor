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
    (re.compile(r"\b(lipstick|mascara|foundation|perfume|cologne|lotion|cosmetic|shampoo|skincare|toner|essence|(?:skin|face|body|moisturizing)\s+cream)\b", re.I),
     ["cosmetics"]),
    (re.compile(r"\b(립스틱|마스카라|파운데이션|향수|로션|화장품|샴푸|스킨케어|토너|에센스)\b"),
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
    (re.compile(r"\b(stir[- ]?fried|fried|cooked|seasoned|prepared)\b.{0,40}\b(octopus|squid|mollusc|cockle|shrimp|prawn|crustacean|fish|seafood)\b|\b(octopus|squid|mollusc|cockle|shrimp|prawn|crustacean|fish|seafood)\b.{0,40}\b(stir[- ]?fried|fried|cooked|seasoned|prepared)\b|낙지.{0,12}볶음|주꾸미.{0,12}볶음|쭈꾸미.{0,12}볶음|오징어.{0,12}볶음|새우.{0,12}볶음|꼬막.{0,12}(장|무침|볶음)", re.I),
     "16", "prepared aquatic animal product"),
    (re.compile(r"\b(noodle|ramen|pasta|macaroni|spaghetti)\b|라면|유탕면|국수|면류|파스타"), "19", "cereal/noodle preparation"),
    (re.compile(r"\b(sauce|seasoning|condiment|soup|broth|stock)\b|소스|양념|조미|스프|미역국|국물|(?<!중)국|탕|찌개|육수"), "21", "miscellaneous edible preparation"),
    (re.compile(r"\b(sausage|ham|surimi)\b|소시지|햄|어묵|맛살|멘보샤"), "16", "meat/fish/crustacean preparation"),
    (re.compile(r"\b(dumpling|mandu|stuffed pasta|stuffed noodles)\b|만두|물만두|군만두"), "19", "stuffed pasta/cereal preparation"),
    (re.compile(r"\b(jam|pickle|fruit preparation|vegetable preparation)\b|잼|절임|피클|과실가공|채소가공"), "20", "vegetable/fruit preparation"),
    (re.compile(r"\b(beverage|drink|juice|tea)\b|음료|주스|차음료"), "22", "beverage"),
    (re.compile(r"\b(deodorant|antiperspirant|roll[- ]?on|cosmetic|skincare|perfume|lotion|shampoo|toner|essence)\b|데오드란트|데오도란트|롤온|화장품|스킨케어|향수|로션|샴푸|토너|에센스"), "33", "cosmetic/toilet preparation"),
]


# Single-word chapter-title leftovers are too broad for routing. Keep phrases
# such as "animal origin" or "toilet preparations"; suppress only standalone
# generic tokens from chapter_keywords. Prepared/raw scope signals are handled
# separately and are not filtered through this set.
_GENERIC_CHAPTER_KEYWORD_STOPLIST: set[str] = {
    "animal",
    "edible",
    "essential",
    "included",
    "miscellaneous",
    "natural",
    "origin",
    "other",
    "prepared",
    "preparation",
    "preparations",
    "produce",
    "product",
    "products",
    "specified",
    "toilet",
    "containing",
    "containers",
}

_RAW_INGREDIENT_CHAPTERS_FOR_PREPARED_FOOD: set[str] = {
    "02",
    "03",
    "04",
    "07",
    "08",
    "09",
    "10",
    "11",
    "12",
    "13",
    "14",
    "15",
}


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
        normalized = re.sub(r"\s+", " ", haystack or "").lower()
        for term in terms:
            term_norm = re.sub(r"\s+", " ", str(term or "").strip().lower())
            if len(term_norm) < 2:
                continue
            # English/number terms use token boundaries to avoid substring
            # false positives. Korean tariff phrases do not have whitespace
            # tokenization, so keep exact phrase containment for Hangul terms.
            if re.fullmatch(r"[a-z0-9][a-z0-9 /'&().-]*", term_norm, flags=re.I):
                pattern = r"(?<![a-z0-9])" + re.escape(term_norm) + r"(?![a-z0-9])"
                matched = re.search(pattern, normalized, flags=re.I) is not None
            else:
                matched = term_norm in normalized
            if matched and term not in out:
                out.append(term)
        return out

    @staticmethod
    def _filter_chapter_keyword_terms(terms: list[str]) -> list[str]:
        filtered: list[str] = []
        for term in terms:
            term_norm = re.sub(r"\s+", " ", str(term or "").strip().lower())
            if not term_norm:
                continue
            if " " not in term_norm and term_norm in _GENERIC_CHAPTER_KEYWORD_STOPLIST:
                continue
            filtered.append(term)
        return filtered

    @staticmethod
    def _flatten_string_values(value: Any) -> list[str]:
        out: list[str] = []
        if value is None:
            return out
        if isinstance(value, dict):
            for item in value.values():
                out.extend(DomainRouterTool._flatten_string_values(item))
            return out
        if isinstance(value, (list, tuple, set)):
            for item in value:
                out.extend(DomainRouterTool._flatten_string_values(item))
            return out
        text = re.sub(r"\s+", " ", str(value or "").strip())
        return [text] if text else []

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
        identity_lane = (
            product_understanding.get("identity_lane")
            if isinstance(product_understanding.get("identity_lane"), dict)
            else {}
        )
        composition_lane = (
            product_understanding.get("composition_lane")
            if isinstance(product_understanding.get("composition_lane"), dict)
            else {}
        )
        classifier_projection = (
            product_understanding.get("classifier_projection")
            if isinstance(product_understanding.get("classifier_projection"), dict)
            else {}
        )
        # Only the structured ProductUnderstanding description is safe for
        # chapter routing. The legacy one-sentence translation is kept for
        # CN retriever compatibility, but it can hallucinate tariff terms like
        # "stuffed" and must not become chapter evidence.
        normalized_description = (
            identity_lane.get("normalized_tariff_description")
            or product_understanding.get("normalized_tariff_description")
            or ""
        )
        keyword_map = product_understanding.get("keyword_map") or {}
        distilled_identity = (
            identity_lane.get("distilled_identity")
            if isinstance(identity_lane.get("distilled_identity"), dict)
            else {}
        )
        excluded_terms = {
            str(item.get("term") or "").strip().lower()
            for item in (
                (product_understanding.get("blocked_routing_terms") or [])
                + (product_understanding.get("excluded_from_routing_terms") or [])
            )
            if isinstance(item, dict) and str(item.get("term") or "").strip()
        }
        structured_terms = self._flatten_string_values({
            "translated_product_name": identity_lane.get("translated_product_name") or product_understanding.get("translated_product_name") or "",
            "commercial_identity": identity_lane.get("commercial_identity") or product_understanding.get("commercial_identity") or "",
            # Never flatten the whole identity_lane. It may contain raw
            # encyclopedia/OCR evidence for audit. DomainRouter may only read
            # compact ProductUnderstanding outputs.
            "distilled_identity": {
                "ingredient_class": distilled_identity.get("ingredient_class") or "",
                "food_form": distilled_identity.get("food_form") or "",
                "processing_state": distilled_identity.get("processing_state") or "",
                "normalized_tariff_description": distilled_identity.get("normalized_tariff_description") or "",
                "identity_terms": distilled_identity.get("identity_terms") or [],
                "composition_terms": distilled_identity.get("composition_terms") or [],
                "processing_terms": distilled_identity.get("processing_terms") or [],
            },
            "identity_lane_compact": {
                "product_form_terms": identity_lane.get("product_form_terms") or [],
                "commodity_identity_terms": identity_lane.get("commodity_identity_terms") or [],
            },
            "composition_lane": {
                "principal_ingredient_terms": composition_lane.get("principal_ingredient_terms") or [],
                "ingredient_taxonomy_terms": composition_lane.get("ingredient_taxonomy_terms") or [],
                "composition_terms": composition_lane.get("composition_terms") or [],
                "processing_terms": composition_lane.get("processing_terms") or [],
            },
            "classifier_route_terms": classifier_projection.get("route_terms") or [],
            "keyword_map": keyword_map,
            "chapter_routing_terms": product_understanding.get("chapter_routing_terms") or [],
            "routing_keywords": product_understanding.get("routing_keywords") or [],
            "routing_terms": product_understanding.get("routing_terms") or [],
            "product_form_terms": product_understanding.get("product_form_terms") or [],
            "processing_terms": product_understanding.get("processing_terms") or [],
            "principal_ingredient_terms": product_understanding.get("principal_ingredient_terms") or [],
            "ingredient_taxonomy_terms": product_understanding.get("ingredient_taxonomy_terms") or [],
            "composition_terms": product_understanding.get("composition_terms") or [],
            "use_context_terms": product_understanding.get("use_context_terms") or [],
            "keywords": product_understanding.get("keywords") or [],
        })
        structured_terms = [
            term for term in structured_terms if term.lower() not in excluded_terms
        ]
        text_parts = [
            product_understanding.get("classification_text") if not normalized_description else "",
            # The LLM translation is useful for the CN retriever, but too
            # noisy for chapter routing: it can hallucinate tariff phrases
            # like "frozen" or "airtight containers" from OCR context.
            identity_lane.get("translated_product_name") or product_understanding.get("translated_product_name") or "",
            identity_lane.get("commercial_identity") or product_understanding.get("commercial_identity") or "",
            normalized_description,
            classifier_projection.get("identity_context") or "",
            classifier_projection.get("composition_context") or "",
            " ".join(structured_terms),
            product_facts.get("product_name") or "",
            product_facts.get("description") or "",
        ]
        haystack = "\n".join(str(p) for p in text_parts if p)
        sauce_product = re.search(
            r"\b(sauce|condiment|seasoning)\b|소스|양념",
            haystack,
            flags=re.I,
        ) is not None
        explicit_noodle_product = re.search(
            r"\b(noodle|ramen|macaroni|spaghetti)\b|라면|유탕면|국수|면류",
            haystack,
            flags=re.I,
        ) is not None
        aquatic_prepared_product = (
            re.search(r"\b(octopus|squid|mollusc|cockle|clam|shrimp|prawn|crustacean|fish|seafood)\b|낙지|주꾸미|쭈꾸미|오징어|꼬막|재첩|새우|대구|고등어|가자미", haystack, flags=re.I)
            and re.search(r"\b(stir[- ]?fried|fried|cooked|seasoned|prepared|preserved|grilled)\b|볶음|무침|구이|조림|장", haystack, flags=re.I)
        )
        soup_or_stew_product = re.search(
            r"\b(soup|broth|stew)\b|(?<!중)국|탕|찌개|전골|육수",
            haystack,
            flags=re.I,
        ) is not None
        processing_state = (
            composition_lane.get("processing_state")
            or product_understanding.get("processing_state")
            or "unknown"
        )
        processed = processing_state == "processed_or_prepared" or bool(
            composition_lane.get("processing_signals")
            or product_understanding.get("processing_signals")
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
            keyword_terms = self._filter_chapter_keyword_terms(
                self._split_values(row.get("chapter_keywords") or "")
            )
            prepared_terms = self._split_values(row.get("prepared_scope_signals") or "")
            raw_terms = self._split_values(row.get("raw_scope_signals") or "")
            domain_terms = self._split_values(row.get("domain_scope_candidates") or "")

            keyword_matches = self._term_matches(keyword_terms, haystack)
            prepared_matches = self._term_matches(prepared_terms, haystack)
            raw_matches = self._term_matches(raw_terms, haystack)

            form_matches: list[str] = []
            for pattern, target_chapter, reason in _PRODUCT_FORM_TO_CHAPTER:
                m = pattern.search(haystack)
                if m and target_chapter == chapter:
                    if (
                        sauce_product
                        and target_chapter == "19"
                        and not explicit_noodle_product
                        and re.search(r"\bpasta\b|파스타", m.group(0), flags=re.I)
                    ):
                        continue
                    form_matches.append(f"{m.group(0)}:{reason}")

            redirects = self._split_values(row.get("prepared_food_redirect_chapters") or "")
            raw_chapter_evidence = bool(keyword_matches or raw_matches)
            guardrail_text = str(row.get("routing_guardrails") or "").lower()
            raw_redirect_guardrail = "before raw ingredient chapter" in guardrail_text
            if (
                processed
                and raw_redirect_guardrail
                and raw_chapter_evidence
                and (raw_matches or prepared_matches)
                and redirects
            ):
                blocked.append({
                    "chapter": chapter,
                    "chapter_title": row.get("chapter_title") or "",
                    "reason": "processed_product_guardrail_redirect",
                    "matched_raw_terms": raw_matches,
                    "matched_prepared_terms": prepared_matches,
                    "redirect_chapters": redirects,
                    "blocking_rule": "processed_or_prepared_product_cannot_be_routed_by_raw_scope_terms",
                })
                for redirect in redirects:
                    redirect_chapter = re.sub(r"\D", "", redirect)[:2].zfill(2)
                    if redirect_chapter:
                        redirect_bonus[redirect_chapter] = redirect_bonus.get(redirect_chapter, 0.0) + 5.0
                # Hard block: official/prepared-vs-raw exclusions are
                # eligibility gates, not small scoring penalties.
                continue

            # Keyword/form evidence creates chapter eligibility. Processing
            # scope terms such as "prepared" or "preparations" only adjust an
            # already plausible chapter; they must not create candidates alone.
            score = float(len(keyword_matches) * 4 + len(form_matches) * 8)
            if score <= 0:
                continue
            if processed:
                score += len(prepared_matches) * 2
                if raw_matches:
                    score -= 2
            else:
                score += len(raw_matches) * 3

            if sauce_product:
                if chapter == "21":
                    score += 4
                elif (
                    chapter == "19"
                    and not explicit_noodle_product
                    and any("pasta" in str(m).lower() or "파스타" in str(m) for m in keyword_matches)
                ):
                    score -= 4
                elif (
                    processed
                    and chapter in _RAW_INGREDIENT_CHAPTERS_FOR_PREPARED_FOOD
                    and not form_matches
                ):
                    blocked.append({
                        "chapter": chapter,
                        "chapter_title": row.get("chapter_title") or "",
                        "reason": "prepared_sauce_raw_ingredient_lowered",
                        "matched_terms": keyword_matches + prepared_matches + raw_matches,
                        "blocking_rule": "sauce_or_condiment_product_should_not_route_by_raw_ingredient_terms",
                    })
                    continue
            if aquatic_prepared_product:
                if chapter == "16":
                    score += 10
                elif chapter == "21" and not soup_or_stew_product:
                    score -= 6
                elif chapter == "19" and not explicit_noodle_product:
                    score -= 4

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
        if not scores and "cosmetics" in domain_hints:
            chapter = "33"
            row = row_by_chapter.get(chapter, {})
            scores[chapter] = {
                "chapter": chapter,
                "chapter_title": row.get("chapter_title") or "",
                "score": 12.0,
                "matched_terms": ["cosmetics_domain_hint"],
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
                "product_understanding_mode": product_understanding.get("product_understanding_mode") or "",
                "used_normalized_tariff_description": bool(normalized_description),
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

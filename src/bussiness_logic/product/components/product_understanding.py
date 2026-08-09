"""Build ProductUnderstandingPackage from reconstructed product input."""

from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Mapping

from bussiness_logic.pipeline.blackboard import BlackboardStore, now_iso
from bussiness_logic.pipeline.component_base import BasePipelineComponent
from bussiness_logic.product.services.identity_hint_agent import IdentityHintAgent
from bussiness_logic.product.model.product_understanding import (
    CompositionExtractionTrace,
    CompositionFactSet,
    CoiEvidenceSet,
    DistilledIdentityFacts,
    EncyclopediaEvidenceSet,
    IdentityHintSet,
    ProductUnderstandingPackage,
)
from bussiness_logic.product.services.encyclopedia_lookup import LookupEncyclopediaEvidence
from bussiness_logic.product.services.identity_distiller import IdentityDistillerService
from bussiness_logic.utils.json_types import JsonValue


PERCENT_RE = re.compile(
    r"(?P<term>[A-Za-z가-힣][A-Za-z가-힣 /·._-]{0,39}?)"
    r"(?:\s*[\(\[（［][^)\]）］]{0,80}[\)\]）］])?"
    r"\s*(?P<percent>\d+(?:[.,]\d+)?)\s*%",
)
COMPONENT_NAME_RE = re.compile(r"[\(\[（［](?P<component>[^)\]）］]+)[)\]）］]")
CONTENT_WEIGHT_RE = re.compile(
    r"(?P<component>[A-Za-z가-힣0-9][A-Za-z가-힣0-9 /·._-]{0,40}?)"
    r"\s*(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<unit>g|kg|ml|l|개)",
    re.I,
)
COMPOSITION_PERCENT_FIELD_MARKERS = (
    "원재료",
    "원료",
    "배합",
    "성분",
    "ingredients",
    "composition",
)
WRAPPER_RE = re.compile(r"만두피|도우|반죽|wrapper|dough|pastry", re.I)
SAUCE_BROTH_RE = re.compile(r"소스|국물|육수|스프|sauce|broth|soup|stock", re.I)
ALLERGEN_RE = re.compile(
    r"알레르|알러지|알레르겐|같은\s*제조시설|동일\s*제조시설|교차|allergen|may contain|cross[- ]?contact",
    re.I,
)
# Acquisition-level noise filters (allowed hardcoding — collection, not judgement):
# origin marks ("중국산 100%") are not ingredient percentages, and admin label
# lines (packaging/expiry/shipping) are not composition terms.
ORIGIN_TERM_RE = re.compile(r"원산지|^[가-힣]{1,4}산$")
ADMIN_LABEL_LINE_RE = re.compile(
    r"^(?:포장타입|중량/?용량|판매단위|소비기한|유통기한|보관\s*방법|배송|교환|반품|고객|원산지)",
)
NUTRITION_FIELD_RE = re.compile(
    r"영양|nutrition|열량|나트륨|탄수화물|당류|지방|콜레스테롤|단백질",
    re.I,
)
COMPONENT_WEIGHT_NOISE_NAMES = frozenset({
    "내",
    "내용량",
    "총내용량",
    "열량",
    "나트륨",
    "탄수화물",
    "당류",
    "지방",
    "트랜스지방",
    "포화지방",
    "콜레스테롤",
    "단백질",
})
INGREDIENT_CLASS_RULES = (
    ("mollusc", ("낙지", "주꾸미", "문어", "오징어", "꼬막", "새꼬막", "재첩", "조개", "mollusc", "octopus", "squid", "clam", "cockle")),
    ("crustacean", ("새우", "게", "crab", "shrimp", "prawn", "lobster", "crustacean")),
    ("fish", ("어묵", "연육", "어육", "대구", "가다랑어", "참치", "fish", "cod", "surimi", "tuna")),
    ("meat", ("돼지고기", "소고기", "닭고기", "pork", "beef", "chicken", "meat")),
    ("cereal", ("밀가루", "밀", "찹쌀", "쌀", "통밀", "메밀", "떡", "면", "우동", "flour", "wheat", "rice", "cereal", "noodle")),
    ("soy_legume", ("서리태", "대두", "콩", "두부", "soy", "soybean", "bean", "tofu")),
    ("vegetable", ("고추", "대파", "양파", "무", "김치", "배추", "부추", "마늘", "나물", "vegetable", "pepper", "radish", "cabbage", "garlic")),
    ("seasoning_sauce", ("소스", "간장", "고추장", "된장", "조미", "양념", "sauce", "broth", "seasoning")),
)
INGREDIENT_TERM_ALIASES = (
    ("찹쌀", ("glutinous rice", "rice")),
    ("쌀", ("rice",)),
    ("밀가루", ("wheat flour", "flour", "wheat")),
    ("통밀", ("whole wheat", "wheat")),
    ("메밀", ("buckwheat",)),
    ("서리태", ("black soybean", "soybean", "bean")),
    ("대두", ("soybean", "soy")),
    ("재첩", ("clam", "corbicula", "mollusc")),
    ("꼬막", ("cockle", "clam", "mollusc")),
    ("새꼬막", ("cockle", "clam", "mollusc")),
    ("낙지", ("octopus", "mollusc")),
    ("주꾸미", ("webfoot octopus", "octopus", "mollusc")),
    ("오징어", ("squid", "mollusc")),
    ("새우", ("shrimp", "prawn", "crustacean")),
    ("대구", ("cod", "fish")),
    ("연육", ("surimi", "fish paste", "fish")),
    ("어육", ("fish",)),
    ("어묵", ("fish cake", "fish")),
)
PROCESSING_STATE_RULES = (
    ("frozen", ("냉동", "-18", "frozen")),
    (
        "requires_cooking",
        (
            "가열하여 섭취",
            "가열 후 섭취",
            "조리하여 섭취",
            "cook before eating",
            "requires cooking",
            "heat before consumption",
        ),
    ),
    (
        "cooked",
        (
            "유탕",
            "볶음",
            "삶은",
            "구운",
            "찐",
            "조리완료",
            "cooked",
            "boiled",
            "fried",
            "roasted",
            "steamed",
        ),
    ),
    ("fermented", ("발효", "김치", "fermented")),
    ("dried", ("건조", "dried")),
    ("chilled", ("냉장", "chilled")),
    ("fresh", ("신선", "fresh")),
    ("uncooked", ("비가열", "uncooked", "raw")),
    ("prepared", ("가공품", "가열하지 않고 섭취", "즉석", "소스", "양념", "prepared")),
)


class ProductUnderstandingComponent(BasePipelineComponent):
    component_name = "Product_Understanding_Component"
    stage = "Product_Understanding"
    llm_model = None

    def __init__(
        self,
        identityHintAgent: IdentityHintAgent | None = None,
    ) -> None:
        super().__init__()
        self._identityHintAgent = identityHintAgent

    def Run(self, store: BlackboardStore) -> None:
        bb = store.load()
        pes = bb.get("product_evidence_state") or {}
        if not isinstance(pes, dict):
            raise RuntimeError("No InputEvidenceState on the Blackboard.")
        productId = str(pes.get("product_id") or "")
        self.ReadBlackBoard(productId)

        observedFacts = pes.get("observed_facts") or {}
        if not isinstance(observedFacts, dict):
            observedFacts = {}
        productName = str(observedFacts.get("product_name") or "")
        shortDescription = str(observedFacts.get("description") or "")
        factTexts = self._ReadTextTuple(
            observedFacts.get("reconstructed_fact_texts")
            or observedFacts.get("composition")
            or [],
        )
        productFacts = self._ReadFactTuple(
            observedFacts.get("reconstructed_product_facts") or [],
        )
        inputReconstruction = observedFacts.get("input_reconstruction") or {}
        if not isinstance(inputReconstruction, dict):
            inputReconstruction = {}
        reconstructedTables = self._ReadFactTuple(
            inputReconstruction.get("reconstructed_tables") or [],
        )
        userIngredients = self._ReadFactTuple(observedFacts.get("ingredients") or [])
        intendedUse = str(observedFacts.get("intended_use") or "unknown").strip()
        intendedUseText = (
            f"intended use: {intendedUse}"
            if intendedUse and intendedUse != "unknown"
            else ""
        )
        classificationText = "\n".join(
            text
            for text in (
                productName,
                shortDescription,
                intendedUseText,
                *factTexts,
                *self._FactTexts(productFacts),
            )
            if text.strip()
        )

        coiEvidence = self._BuildCoiEvidenceSet(
            store,
            productId=productId,
            productName=productName,
        )
        # [8회차-1] 등급 승선제 원천: 법정 어휘 = cn_chapter_index 기계
        # 도출(_chapter_vocab — 수기 목록 0) · 사전 표제 = 관세청 표준품명
        # (term_bridge _load_dict ko 표제). 실패 시 빈 집합 = 전부 약 등급
        # (기록 보존, 승선만 보수적).
        try:
            from bussiness_logic.product.services.identity_hint_agent import _scoped_chapter_vocab
            _legal_vocab = _scoped_chapter_vocab()
        except Exception:  # noqa: BLE001
            _legal_vocab = frozenset()
        try:
            from bussiness_logic.product.services.term_bridge import _load_dict, _norm_key
            _dict_titles = frozenset(
                _norm_key(r.get("ko") or "") for r in _load_dict() if r.get("ko"))
        except Exception:  # noqa: BLE001
            _dict_titles = frozenset()
        encyclopediaEvidence = LookupEncyclopediaEvidence(
            encyclopediaEvidenceId=store.next_id("ency"),
            legalVocab=_legal_vocab,
            dictTitles=_dict_titles,
            productId=productId,
            query=productName,
        )
        distilledIdentity = IdentityDistillerService().BuildFacts(
            distilledIdentityId=store.next_id("distid"),
            productId=productId,
            encyclopediaEvidence=encyclopediaEvidence,
        )
        identity = self._BuildIdentitySeed(
            identityHintId=store.next_id("hint"),
            productId=productId,
            distilledIdentity=distilledIdentity,
        )
        identity = self._MaybeEnrichIdentityWithLlm(
            identity,
            productName=productName,
            distilledIdentity=distilledIdentity,
            encyclopediaEvidence=encyclopediaEvidence,
            factTexts=factTexts,
        )
        # 식품유형 결정론 전사: 라벨의 법정 표기('식품유형: 어묵(...)')를
        # identity에 그대로 병기 — LLM 산출이 런마다 이 fact를 뽑다 말다
        # 하는 실측(22건 중 변형/누락 10) 때문에 출력 보장은 전사가 담당.
        # 한→영은 DB의 법정 유한 어휘 사전(food_type_dictionary,
        # 수기 교정 우선). 창작 0 — 존재할 때만.
        identity = self._TranscribeFoodTypeFact(identity, factTexts=factTexts)
        # 보관상태(냉동/냉장/실온) 병기 — LLM 누락 대비 결정론 전사
        identity = self._TranscribeStorageState(identity, factTexts=factTexts)
        # [ntd 조립 헤드 · 2026-07-23 설계자 승인] ntd 자유작문 제한의
        # 짝: LLM ntd는 어휘집 게이트(identity_hint_agent)로 줄이고, 라벨
        # 전사 EN(식품유형 사전·보관상태)을 ntd **앞에 병기**한다(prepend
        # — 덮어쓰기 아님·합성어 구문 보존). 22셋 실측: 식품유형 사전히트
        # 13/22·보관 11/22 — 과반의 ntd 헤드가 매런 동일해진다.
        identity = self._AssembleNtdHead(identity, factTexts=factTexts)
        if intendedUse and intendedUse != "unknown":
            identity = dataclasses.replace(
                identity,
                intendedUse=intendedUse,
                identityTerms=self._DedupStrings(
                    [*identity.identityTerms, intendedUse],
                    limit=80,
                ),
            )
        composition = self._BuildCompositionLane(
            factTexts=factTexts,
            productFacts=productFacts,
            reconstructedTables=reconstructedTables,
            coiEvidence=coiEvidence,
            userIngredients=userIngredients,
        )
        # [8회차-1 (다)] 백과→조성 경로 개통 — 승선(강/중) 문서의 재질
        # 서술 문장에서 법정 어휘 실등장분만 조성 토큰으로 (볼펜 steel/
        # brass/tungsten carbide 유실 실측의 처방). 문장 탐지는 문법
        # 패턴((나)급 — 규칙 대장 등재), 어휘 자격은 legalVocab(기계 도출)
        # — 수기 도메인 어휘 0, 창작 0.
        try:
            _enc_terms, _enc_traces = self._EncyclopediaMaterialTerms(
                encyclopediaEvidence, self._MaterialVocab() or _legal_vocab)
            if _enc_terms:
                import dataclasses as _dc
                composition = _dc.replace(
                    composition,
                    compositionTerms=tuple(dict.fromkeys(
                        (*composition.compositionTerms, *_enc_terms))),
                    extractionTraces=(
                        *composition.extractionTraces, *_enc_traces),
                )
        except Exception:  # noqa: BLE001 — 백과 조성 실패는 무영향
            pass
        understandingId = store.next_id("under")
        productUnderstanding = ProductUnderstandingPackage(
            understandingId=understandingId,
            productId=productId,
            sourceProductId=productId,
            productName=productName,
            shortDescription=shortDescription,
            classificationText=classificationText,
            reconstructedFactTexts=factTexts,
            reconstructedProductFacts=productFacts,
            distilledIdentity=distilledIdentity,
            identityHints=identity,
            compositionFacts=composition,
            coiEvidence=coiEvidence,
            encyclopediaEvidence=encyclopediaEvidence,
            routingTerms=self._RoutingTerms(
                productName=productName,
                distilledIdentity=distilledIdentity,
                identity=identity,
            ),
            unknowns=(
                ("reconstructed_product_facts",)
                if not productFacts
                else ()
            ),
        )
        store.put(
            "product_understanding",
            productUnderstanding.ToBlackboard(
                createdBy=self.component_name,
                createdAt=now_iso(),
            ),
        )
        # 정규화 COI 폼 가산 주입 (ASAP_COI_FORM_DIR 설정 시에만 — 기본 no-op).
        # 주입 규칙·방어(교차 합의제, sauce_only 주성분 금지)는 coi_form_injector.
        try:
            from bussiness_logic.product.services.coi_loader import (
                InjectIntoProductUnderstanding,
            )
            bb = store.load()
            pu_dict = bb.get("product_understanding")
            if isinstance(pu_dict, dict):
                injected = InjectIntoProductUnderstanding(pu_dict)
                # 용어 브리지 — COI 유무와 무관하게 entries 완성 직후 1회
                # (자유어→법정어 수렴, ASAP_TERM_BRIDGE 게이트)
                from bussiness_logic.product.services.term_bridge import ApplyTermBridge
                bridged = ApplyTermBridge(pu_dict)
                if injected or bridged:
                    bb["product_understanding"] = pu_dict
                    store.save(bb)
                    self.reason(f"정규화 COI 주입: {injected} | 브리지: {bridged}")
        except Exception as error:  # noqa: BLE001 — 주입 실패는 파이프라인 무영향
            self.reason(f"정규화 COI 주입 생략: {type(error).__name__}")
        self.WriteBlackBoard(understandingId)
        self.reason(
            "ProductUnderstandingPackage 생성: "
            f"facts={len(productFacts)}, fact_texts={len(factTexts)}, "
            f"encyclopedia={encyclopediaEvidence.qualityStatus}, "
            f"composition_terms={len(composition.compositionTerms)}."
        )

    @staticmethod
    def _TranscribeFoodTypeFact(identity, *, factTexts):
        import unicodedata as _ud
        from dataclasses import replace as _replace
        for raw in factTexts or ():
            s = _ud.normalize("NFC", str(raw))
            m = re.match(r"^\s*(?:식품\s*의?\s*유형|제품\s*의?\s*유형|품목\s*보고[^:：]*)[^:：]*[:：]\s*(.+)", s)
            if not m:
                continue
            main = re.split(r"[\(（/,;]", m.group(1))[0].strip()
            if not main or len(main) > 20:
                continue
            en = ""
            try:
                from bussiness_logic.core.runtime_asset_repository import (
                    LoadFoodTypeDictionary,
                )
                _fd = LoadFoodTypeDictionary()
                # 정확매칭 우선, 없으면 부분매칭(긴 키 우선) — 식품유형이
                # 복합/포괄('만두류 및 소스류'·'기타 수산물가공품')일 때 핵심어
                # 포착(만두류→dumplings). 창작 0 — 사전 등재 어휘만.
                en = str(_fd.get(main) or "")
                if not en:
                    for _k, _v in sorted(_fd.items(), key=lambda p: -len(p[0])):
                        if len(_k) >= 2 and _k in main:
                            en = str(_v)
                            break
            except Exception:  # noqa: BLE001 — 사전 부재는 한글 전사만
                en = ""
            toks = [w for w in (main, *str(en).split()) if w]
            terms = tuple(dict.fromkeys([*identity.identityTerms, *toks]))
            food_form = identity.foodForm
            if en and (not food_form or food_form in ("other", "unknown")):
                food_form = en
            return _replace(identity, identityTerms=terms, foodForm=food_form)
        return identity

    @staticmethod
    def _TranscribeStorageState(identity, *, factTexts):
        """보관상태(냉동/냉장/실온) 결정론 전사 → preservationState +
        identity/form 토큰. 닫힌 3상태 맵(자의적 사전 아님·창작 0). LLM이
        런마다 놓치는 보존상태를 라벨에서 병기 — 냉동→0710 등 보존축 매칭."""
        import unicodedata as _ud
        from dataclasses import replace as _replace
        _STATE = (("냉동", "frozen"), ("냉장", "chilled"),
                  ("실온", "ambient"), ("상온", "ambient"))
        for raw in factTexts or ():
            s = _ud.normalize("NFC", str(raw))
            m = re.match(
                r"^\s*(?:보관\s*상태|보관\s*방법|보관|포장\s*타입|포장\s*상태)"
                r"[^:：]*[:：]\s*(.+)", s)
            if not m:
                continue
            en = next((v for k, v in _STATE if k in m.group(1)), "")
            if not en:
                continue
            terms = tuple(dict.fromkeys([*identity.identityTerms, en]))
            forms = tuple(dict.fromkeys([*identity.productFormTerms, en]))
            pres = identity.preservationState
            if not pres or pres in ("unknown", ""):
                pres = en
            return _replace(identity, identityTerms=terms,
                            productFormTerms=forms, preservationState=pres)
        return identity

    @staticmethod
    def _AssembleNtdHead(identity, *, factTexts):
        """[ntd 조립 헤드] 라벨 전사 EN을 ntd 앞에 결정론 병기.

        헤드 = [식품유형 사전 EN 구문] + [보관상태 EN]. 둘 다 라벨 존재
        시에만(창작 0), 구문 통째 보존(합성어 파괴 금지 — 'fish cake'를
        쪼개지 않음). LLM ntd(어휘집 게이트 통과분)는 뒤에 유지 — 덮어
        쓰기 아님. 라벨 없으면 무변경."""
        import unicodedata as _ud
        from dataclasses import replace as _replace
        head: list = []
        # ① 식품유형 → 사전 EN 구문 (search — '옵션 N' 접두 무관)
        for raw in factTexts or ():
            s = _ud.normalize("NFC", str(raw))
            m = re.search(
                r"(?:식품\s*의?\s*유형|제품\s*의?\s*유형|품목\s*보고[^:：]*)"
                r"[^:：]*[:：]\s*(.+)", s)
            if not m:
                continue
            main = re.split(r"[\(（/,;]", m.group(1))[0].strip()
            if not main or len(main) > 20:
                continue
            try:
                from bussiness_logic.core.runtime_asset_repository import (
                    LoadFoodTypeDictionary,
                )
                _fd = LoadFoodTypeDictionary()
                en = str(_fd.get(main) or "")
                if not en:
                    for _k, _v in sorted(_fd.items(), key=lambda p: -len(p[0])):
                        if len(_k) >= 2 and _k in main:
                            en = str(_v)
                            break
                if en:
                    head.append(en)
                    break
            except Exception:  # noqa: BLE001 — 사전 부재 = 헤드 없음
                break
        # ② 보관상태 EN (전사 결과 재사용 — preservationState가 이미 병기됨)
        pres = str(identity.preservationState or "").strip()
        if pres and pres not in ("unknown",):
            head.append(pres)
        if not head:
            return identity
        tail = str(identity.normalizedTariffDescription or "").strip()
        parts = [*head, tail] if tail else head
        # 중복 구문 제거(순서 보존)
        joined = "; ".join(dict.fromkeys(parts))
        return _replace(identity, normalizedTariffDescription=joined)

    def _BuildCoiEvidenceSet(
        self,
        store: BlackboardStore,
        *,
        productId: str,
        productName: str,
    ) -> CoiEvidenceSet:
        # [11회차-2 §1-A] 구식 COI 경로 소멸 — LoadCoiEvidence(ASAP_COI_ROOT
        # 디렉토리 스캔·상품명 유사도 매칭)는 정규화 폼 단일화(coi_loader
        # InjectIntoProductUnderstanding/ParseCoiComposition)로 대체됐다.
        # 삭제 근거: 최근 2런 58건 blackboard에 구식 경로 서명 0(coi_root·
        # LoadCoiEvidence 흔적 0) — 원장·규칙 대장 기록.
        # 본 메서드는 빈 CoiEvidenceSet만 반환(주입은 정규화 폼 경로 전담).
        return CoiEvidenceSet(
            coiEvidenceId=store.next_id("coi"),
            productId=productId,
        )

    @staticmethod
    def _ReadTextTuple(value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            return (value.strip(),) if value.strip() else ()
        if not isinstance(value, list):
            return ()
        return tuple(str(item).strip() for item in value if str(item).strip())

    @staticmethod
    def _ReadFactTuple(value: object) -> tuple[dict[str, JsonValue], ...]:
        if not isinstance(value, list):
            return ()
        return tuple(
            ProductUnderstandingComponent._JsonDict(item)
            for item in value
            if isinstance(item, dict)
        )

    @staticmethod
    def _JsonDict(value: Mapping[object, object]) -> dict[str, JsonValue]:
        out: dict[str, JsonValue] = {}
        for key, item in value.items():
            if isinstance(key, str):
                out[key] = ProductUnderstandingComponent._JsonValue(item)
        return out

    @staticmethod
    def _JsonValue(value: object) -> JsonValue:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, list):
            return [
                ProductUnderstandingComponent._JsonValue(item)
                for item in value
            ]
        if isinstance(value, dict):
            return ProductUnderstandingComponent._JsonDict(value)
        return str(value)

    @staticmethod
    def _FactTexts(productFacts: tuple[dict[str, JsonValue], ...]) -> tuple[str, ...]:
        texts: list[str] = []
        for fact in productFacts:
            field = str(
                fact.get("field_name")
                or fact.get("field")
                or fact.get("name")
                or ""
            ).strip()
            value = str(
                fact.get("normalized_value")
                or fact.get("value")
                or fact.get("text")
                or ""
            ).strip()
            if field and value:
                texts.append(f"{field}: {value}")
            elif value:
                texts.append(value)
        return tuple(texts)

    @staticmethod
    def _BuildIdentitySeed(
        *,
        identityHintId: str,
        productId: str,
        distilledIdentity: DistilledIdentityFacts,
    ) -> IdentityHintSet:
        return IdentityHintSet(
            identityHintId=identityHintId,
            productId=productId,
            commercialIdentity=distilledIdentity.commercialIdentity,
            normalizedTariffDescription=distilledIdentity.normalizedDescription,
            identityTerms=distilledIdentity.identityTerms,
            productFormTerms=distilledIdentity.productFormSignalTerms,
            confidence=0.4 if distilledIdentity.identityTerms else 0.0,
            understandingMode="wikipedia_distilled",
        )

    def _MaybeEnrichIdentityWithLlm(
        self,
        identity: IdentityHintSet,
        *,
        productName: str,
        distilledIdentity: DistilledIdentityFacts,
        encyclopediaEvidence: EncyclopediaEvidenceSet,
        factTexts: tuple[str, ...] = (),
    ) -> IdentityHintSet:
        """Overlay bounded LLM identity fields unless explicitly disabled.

        On by default (``ASAP_USE_LLM_UNDERSTANDING``). On LLM failure the regex identity is
        returned with the error recorded in the identity hint reasons. LLM output is
        already vocab-validated, so the overlay cannot introduce codes.
        """
        flag = (os.environ.get("ASAP_USE_LLM_UNDERSTANDING", "1") or "").strip().lower()
        if flag not in ("1", "true", "yes", "on"):
            return identity

        if self._identityHintAgent is None:
            return dataclasses.replace(
                identity,
                understandingMode="llm_fallback",
                llmError="identity_hint_runtime_not_configured",
            )

        result = self._identityHintAgent.BuildIdentityFacts(
            productName=productName,
            distilledIdentity=distilledIdentity,
            encyclopediaEvidence=encyclopediaEvidence,
            factTexts=factTexts,
        )
        if result.get("understanding_mode") != "llm_json":
            return dataclasses.replace(
                identity,
                understandingMode=str(result.get("understanding_mode") or "regex_fallback"),
                llmError=str(result.get("llm_error") or ""),
            )
        overlay: dict[str, object] = {
            "productFormTerms": result["product_form_terms"],
            "domainHints": result["domain_hints"],
            "chapterHintTerms": result["chapter_hint_terms"],
            "chapterHintSourceTerms": result["chapter_hint_source_terms"],
            "chapterHintBasis": result["chapter_hint_basis"],
            "chapterHintStatus": result["chapter_hint_status"],
            "translatedProductName": result["translated_product_name"],
            "confidence": result["confidence"],
            "needsReview": result["needs_review"],
            "conflictReason": str(result.get("conflict_reason") or ""),
            "understandingMode": "llm_json",
            "llmError": "",
        }
        # Prefer non-empty LLM text/lists; keep the regex value otherwise.
        # Typed identity fields: vocab-grounded upstream; empty means the
        # evidence did not support a value, so the DTO default stands.
        if result.get("ingredient_class"):
            overlay["ingredientClass"] = result["ingredient_class"]
        if result.get("principal_ingredient_guess"):
            overlay["principalIngredientGuess"] = result["principal_ingredient_guess"]
        if result.get("accessory_ingredients"):
            overlay["accessoryIngredients"] = tuple(result["accessory_ingredients"])
        if result.get("food_form"):
            overlay["foodForm"] = result["food_form"]
        if result.get("processing_state"):
            overlay["processingState"] = result["processing_state"]
        if result.get("preservation_state"):
            overlay["preservationState"] = result["preservation_state"]
        if result.get("physical_form"):
            overlay["physicalForm"] = result["physical_form"]
        if result["commercial_identity"]:
            overlay["commercialIdentity"] = result["commercial_identity"]
        if result["normalized_tariff_description"]:
            overlay["normalizedTariffDescription"] = result["normalized_tariff_description"]
        if result["identity_terms"]:
            overlay["identityTerms"] = result["identity_terms"]
        if result.get("title_bound_terms"):
            overlay["titleBoundTerms"] = tuple(result["title_bound_terms"])
        return dataclasses.replace(identity, **overlay)

    _material_vocab_cache: list = []

    @classmethod
    def _MaterialVocab(cls) -> frozenset:
        """재질 추출 어휘 — taxonomy table material_composition 패턴의
        리터럴 대안을 기계 파싱 (수기 0, 원천 table 추종). 승선 스코프
        어휘(소유 챕터 ≤3)는 다챕터 재질(steel 등)을 정당하게 배제하므로
        재질 추출에는 패턴 실소유분을 쓴다."""
        if cls._material_vocab_cache:
            return cls._material_vocab_cache[0]
        vocab: set[str] = set()
        try:
            from bussiness_logic.core.runtime_asset_repository import (
                LoadClassificationCriterionTaxonomy,
            )
            for _r in LoadClassificationCriterionTaxonomy():
                if str(_r.get("criterion_type")) != "material_composition":
                    continue
                for _grp in re.findall(r"\(\?\:([^()]+)\)", str(_r.get("examples") or "")):
                    for _alt in _grp.split("|"):
                        _w = re.sub(r"[^a-z]", "", _alt.lower().replace("s?", ""))
                        if len(_w) >= 3:
                            vocab.add(_w)
        except Exception:  # noqa: BLE001
            vocab = set()
        cls._material_vocab_cache.append(frozenset(vocab))
        return cls._material_vocab_cache[0]

    _MATERIAL_SENT_RX = re.compile(
        r"(?:made\s+(?:up\s+)?of|consist(?:s|ing)?\s+of|composed\s+of|"
        r"materials?\s+(?:commonly\s+)?used)", re.I)
    _MATERIAL_TOK_RX = re.compile(r"[a-z]{3,}")
    _MATERIAL_SPLIT_RX = re.compile(r"(?<=[.!?])\s+")

    @staticmethod
    def _EncyclopediaMaterialTerms(
        encyclopediaEvidence: EncyclopediaEvidenceSet,
        legalVocab: frozenset,
    ) -> tuple[tuple[str, ...], tuple[CompositionExtractionTrace, ...]]:
        """승선 문서의 재질 문장 → 법정 어휘 실등장 토큰만 조성 승선."""
        terms: list[str] = []
        traces: list[CompositionExtractionTrace] = []
        if not legalVocab:
            return (), ()
        C = ProductUnderstandingComponent
        for entry in encyclopediaEvidence.entries:
            if str(getattr(entry, "grade", "") or "") not in ("strong", "medium"):
                continue
            for sent in C._MATERIAL_SPLIT_RX.split(str(entry.description or "")):
                if not C._MATERIAL_SENT_RX.search(sent):
                    continue
                hits = [w for w in C._MATERIAL_TOK_RX.findall(sent.lower())
                        if w in legalVocab and w not in terms]
                if not hits:
                    continue
                terms.extend(hits)
                traces.append(CompositionExtractionTrace(
                    sourceFieldName="encyclopedia_evidence",
                    sourceText=sent[:200],
                    selectedSpan=", ".join(hits[:8]),
                    outputField="composition_terms",
                    normalizedValue=", ".join(hits[:8]),
                    extractionMethod="encyclopedia_material_sentence",
                    decisionReason="material sentence x legal vocab (grade "
                                   + str(getattr(entry, "grade", "")) + ")",
                    sourceRefs=(entry.link,),
                ))
        return tuple(terms[:12]), tuple(traces)

    @staticmethod
    def _BuildCompositionLane(
        *,
        factTexts: tuple[str, ...],
        productFacts: tuple[dict[str, JsonValue], ...],
        coiEvidence: CoiEvidenceSet,
        reconstructedTables: tuple[dict[str, JsonValue], ...] = (),
        userIngredients: tuple[dict[str, JsonValue], ...] = (),
    ) -> CompositionFactSet:
        # COI (식품원재료풀이) is composition evidence — it belongs to this lane,
        # not the identity lane. Feed its matched texts into %-parsing and terms.
        coiTexts = tuple(coiEvidence.matchedTexts)
        # 구조화 COI(entries)가 성립하면 평탄 텍스트 유입은 차단 — 부수 성분
        # 전체('starch','설탕')가 광역 어휘 풀로 흘러 1903/2009류를 밀었던
        # 실측의 처방. entries 미성립 시엔 기존대로(정보 손실 방지).
        # ASAP_COI_FLAT_TEXT=1 로 이전 동작 복귀.
        _coi_flat_allowed = (os.environ.get(
            "ASAP_COI_FLAT_TEXT", "0") or "0").strip() == "1"
        if not _coi_flat_allowed:
            # 전면 차단: 파싱 실패 파일의 평탄 텍스트도 잡음 우세 실측
            # (밀면 'starch'→1903, 산채→2009). 정보는 구조화 entries로만.
            coiTexts = ()
        tableTexts = ProductUnderstandingComponent._ReconstructedTableTexts(
            reconstructedTables,
        )
        factTextValues = ProductUnderstandingComponent._FactTexts(productFacts)
        userIngredientTexts = tuple(
            f"{item.get('name')}: {item.get('percentage')}%"
            for item in userIngredients
            if str(item.get("name") or "").strip()
        )
        text = "\n".join(
            [*factTexts, *factTextValues, *tableTexts, *coiTexts, *userIngredientTexts]
        )
        extractionTraces: list[CompositionExtractionTrace] = []
        ProductUnderstandingComponent._AppendNutritionPercentExclusionTraces(
            reconstructedTables=reconstructedTables,
            extractionTraces=extractionTraces,
        )
        ingredientEntries = ProductUnderstandingComponent._BuildIngredientEntries(
            productFacts=productFacts,
            reconstructedTables=reconstructedTables,
            extractionTraces=extractionTraces,
        )
        # COI 구조화 합류 (ASAP_COI_COMPOSITION, 기본 ON): 원료풀이 표를
        # 표기순 엔트리로 파싱해 composition lane에 공급 — 텍스트 잡탕이
        # 아니라 질문이 소비 가능한 구조로. 라벨 엔트리가 없으면 주 소스로
        # 승격(scope=product), 있으면 보조(scope=coi) 병기.
        if (os.environ.get("ASAP_COI_COMPOSITION", "1") or "1").strip() != "0":
            try:
                from bussiness_logic.product.services.coi_loader import ParseCoiComposition
                from pathlib import Path as _P

                coi_entries: list[dict[str, JsonValue]] = []
                for doc in coiEvidence.matchedDocuments[:1]:
                    for e in ParseCoiComposition(_P(doc)):
                        coi_entries.append({
                            "scope": "product" if not ingredientEntries else "coi",
                            "ingredient_name": e["ingredient_name"],
                            "component": e.get("component") or "",
                            "percent": e.get("percent"),
                            "order_index": e["order_index"],
                            "origin": e.get("origin") or "",
                            "source": "coi",
                        })
                if coi_entries:
                    ingredientEntries = [*ingredientEntries, *coi_entries]
            except Exception:  # noqa: BLE001 — COI 실패는 무증거일 뿐
                pass
        evidenceIngredientEntries = list(ingredientEntries)
        userIngredientEntries = ProductUnderstandingComponent._BuildUserIngredientEntries(
            userIngredients=userIngredients,
            extractionTraces=extractionTraces,
        )
        ingredientEntries = ProductUnderstandingComponent._DedupIngredientEntries(
            [*ingredientEntries, *userIngredientEntries],
        )
        percentages = ProductUnderstandingComponent._IngredientPercentagesFromEntries(
            ingredientEntries,
        )
        componentCompositions = ProductUnderstandingComponent._BuildComponentCompositions(
            productFacts=productFacts,
            reconstructedTables=reconstructedTables,
            ingredientEntries=ingredientEntries,
        )
        evidencePrincipalCandidates = (
            ProductUnderstandingComponent._BuildPrincipalCandidates(
                evidenceIngredientEntries,
            )
        )
        evidencePrincipalStatus = ProductUnderstandingComponent._PrincipalStatus(
            evidencePrincipalCandidates,
        )
        principalCandidates = ProductUnderstandingComponent._BuildPrincipalCandidates(
            ingredientEntries,
        )
        principalStatus = ProductUnderstandingComponent._PrincipalStatus(
            principalCandidates,
        )
        if evidencePrincipalStatus != "unknown":
            principalCandidates = evidencePrincipalCandidates
            principalStatus = evidencePrincipalStatus
        # COI 교차 검증 승격: 라벨 1위 후보와 COI 1위 성분의 정규화 토큰이
        # 겹치면 독립 2근거 일치 → confirmed 승격. 한↔영 혼재 파일이 있어
        # 겹침 실패는 conflict가 아니라 '비교 불가'(중립)로 둔다.
        if principalStatus != "confirmed" and principalCandidates:
            _tok = lambda s: {w.lower() for w in re.findall(r"[A-Za-z가-힣]+", str(s or "")) if len(w) >= 2}
            coi_first = next(
                (e for e in ingredientEntries
                 if e.get("source") == "coi" and int(e.get("order_index") or 0) == 1),
                None,
            )
            if coi_first is not None:
                top = principalCandidates[0]
                overlap = _tok(top.get("ingredient_name")) & _tok(coi_first.get("ingredient_name"))
                if overlap:
                    principalStatus = "confirmed"
                    top = dict(top)
                    top["basis"] = f"{top.get('basis')}+coi_cross_check"
                    top["confidence"] = max(float(top.get("confidence") or 0), 0.85)
                    principalCandidates = [top, *principalCandidates[1:]]
        principalIngredient = (
            str(principalCandidates[0].get("ingredient_name") or "")
            if principalStatus == "confirmed" and principalCandidates
            else ""
        )
        userPrimaryIngredient = next(
            (
                str(item.get("name") or "").strip()
                for item in userIngredients
                if item.get("role") == "primary"
                and str(item.get("name") or "").strip()
            ),
            "",
        )
        if evidencePrincipalStatus == "unknown" and userPrimaryIngredient:
            principalIngredient = userPrimaryIngredient
            principalStatus = "user_provided"
            declaredCandidate = next(
                (
                    dict(candidate)
                    for candidate in principalCandidates
                    if str(candidate.get("ingredient_name") or "").casefold()
                    == userPrimaryIngredient.casefold()
                ),
                {"ingredient_name": userPrimaryIngredient},
            )
            declaredCandidate.update({
                "basis": "user_declared_primary",
                "confidence": 1.0,
                "role": "primary",
                "scope": "product",
                "source_refs": ["user_input:ingredients"],
            })
            principalCandidates = [
                declaredCandidate,
                *(
                    candidate
                    for candidate in principalCandidates
                    if str(candidate.get("ingredient_name") or "").casefold()
                    != userPrimaryIngredient.casefold()
                ),
            ]

        compositionTerms = ProductUnderstandingComponent._CompositionTerms(
            productFacts=productFacts,
            reconstructedTables=reconstructedTables,
            factTexts=factTexts,
            coiTexts=coiTexts,
        )
        compositionTerms = ProductUnderstandingComponent._DedupStrings(
            [*compositionTerms, *userIngredientTexts],
            limit=80,
        )
        allergenTexts = [
            item
            for item in factTexts
            if ALLERGEN_RE.search(item)
        ]
        missing: list[str] = []
        if not percentages:
            missing.append("ingredient_percentages")
            reason = (
                "no_top_level_ingredient_percentages"
                if any("%" in term for term in compositionTerms)
                else "ingredient_section_has_no_percent"
            )
            extractionTraces.append(
                CompositionExtractionTrace(
                    sourceFieldName="composition_lane",
                    sourceText="\n".join(compositionTerms[:5]),
                    outputField="ingredient_percentages",
                    extractionMethod="regex_percent",
                    decisionReason=reason,
                    unresolvedReason=reason,
                ),
            )
        ingredientClasses = ProductUnderstandingComponent._BuildIngredientClasses(
            ingredientEntries=ingredientEntries,
            compositionTerms=compositionTerms,
        )
        processingState = ProductUnderstandingComponent._BuildProcessingState(
            factTexts=(
                *factTextValues,
                *tableTexts,
                *factTexts,
                *coiTexts,
            ),
        )
        return CompositionFactSet(
            processingState=processingState,
            principalIngredient=principalIngredient,
            principalIngredientStatus=principalStatus,
            principalIngredientCandidates=tuple(principalCandidates[:10]),
            ingredientClasses=ingredientClasses,
            ingredientEntries=tuple(ingredientEntries[:80]),
            ingredientPercentages=tuple(percentages[:20]),
            componentCompositions=tuple(componentCompositions[:20]),
            compositionTerms=compositionTerms,
            compositionBasis=(
                "mixed"
                if userIngredientEntries and evidenceIngredientEntries
                else "user_input"
                if userIngredientEntries
                else "label"
                if percentages
                else "coi_text"
                if coiTexts
                else "label_text_no_percent"
            ),
            containsWrapperOrDough=bool(WRAPPER_RE.search(text)),
            containsSauceOrBroth=bool(SAUCE_BROTH_RE.search(text)),
            allergenTermsExcluded=tuple(allergenTexts[:20]),
            missingCompositionFacts=tuple(missing),
            extractionTraces=tuple(extractionTraces[:120]),
        )

    @staticmethod
    def _CompositionPercentageTexts(
        productFacts: tuple[dict[str, JsonValue], ...],
        *,
        productLevelOnly: bool = False,
    ) -> tuple[str, ...]:
        texts: list[str] = []
        for fact in productFacts:
            field = str(
                fact.get("field_name")
                or fact.get("field")
                or fact.get("name")
                or ""
            ).strip()
            value = str(
                fact.get("normalized_value")
                or fact.get("value")
                or fact.get("text")
                or ""
            ).strip()
            if not field or not value:
                continue
            if not ProductUnderstandingComponent._IsCompositionFieldName(field):
                continue
            if (
                productLevelOnly
                and not ProductUnderstandingComponent._IsProductLevelCompositionFieldName(field)
            ):
                continue
            texts.append(value)
        return tuple(texts)

    @staticmethod
    def _ReconstructedTableTexts(
        reconstructedTables: tuple[dict[str, JsonValue], ...],
    ) -> tuple[str, ...]:
        texts: list[str] = []
        for table in reconstructedTables:
            tableName = str(table.get("table_name") or "").strip()
            rows = table.get("rows")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                field = str(row.get("field_name") or "").strip()
                value = str(row.get("normalized_value") or "").strip()
                if not field or not value:
                    continue
                prefix = f"{tableName} / " if tableName else ""
                texts.append(f"{prefix}{field}: {value}")
        return tuple(texts)

    @staticmethod
    def _CompositionTerms(
        *,
        productFacts: tuple[dict[str, JsonValue], ...],
        reconstructedTables: tuple[dict[str, JsonValue], ...],
        factTexts: tuple[str, ...],
        coiTexts: tuple[str, ...],
    ) -> tuple[str, ...]:
        terms: list[str] = []
        for fact in productFacts:
            field = ProductUnderstandingComponent._ReadTextField(fact, "field_name")
            value = ProductUnderstandingComponent._ReadTextField(fact, "normalized_value")
            if field and value and ProductUnderstandingComponent._IsCompositionFieldName(field):
                terms.append(f"{field}: {value}")

        for table in reconstructedTables:
            tableName = str(table.get("table_name") or "").strip()
            rows = table.get("rows")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                field = ProductUnderstandingComponent._ReadTextField(row, "field_name")
                value = ProductUnderstandingComponent._ReadTextField(row, "normalized_value")
                if not field or not value:
                    continue
                if not ProductUnderstandingComponent._IsCompositionFieldName(field):
                    continue
                prefix = f"{tableName} / " if tableName else ""
                terms.append(f"{prefix}{field}: {value}")

        for text in factTexts:
            if ProductUnderstandingComponent._LooksLikeCompositionText(text):
                terms.append(text)
        terms.extend(coiTexts)
        return ProductUnderstandingComponent._DedupStrings(terms, limit=80)

    @staticmethod
    def _LooksLikeCompositionText(text: str) -> bool:
        value = str(text or "").strip()
        if not value or ALLERGEN_RE.search(value) or ADMIN_LABEL_LINE_RE.search(value):
            return False
        fieldName = value.split(":", 1)[0].strip() if ":" in value else value
        return ProductUnderstandingComponent._IsCompositionFieldName(fieldName)

    @staticmethod
    def _BuildIngredientClasses(
        *,
        ingredientEntries: list[dict[str, JsonValue]],
        compositionTerms: tuple[str, ...],
    ) -> tuple[str, ...]:
        evidenceTexts = [
            str(entry.get("ingredient_name") or "")
            for entry in ingredientEntries
            if str(entry.get("ingredient_name") or "").strip()
        ]
        if not evidenceTexts:
            evidenceTexts.extend(compositionTerms)
        classes: list[str] = []
        for className, terms in INGREDIENT_CLASS_RULES:
            if any(
                ProductUnderstandingComponent._ContainsTerm(text, term)
                for text in evidenceTexts
                for term in terms
            ):
                classes.append(className)
        return tuple(classes)

    @staticmethod
    def _BuildProcessingState(*, factTexts: tuple[str, ...]) -> str:
        states: list[str] = []
        for stateName, terms in PROCESSING_STATE_RULES:
            if any(
                ProductUnderstandingComponent._ContainsProcessingTerm(text, term)
                for text in factTexts
                for term in terms
            ):
                states.append(stateName)
        return " ".join(states[:4]) if states else "unknown"

    @staticmethod
    def _ContainsProcessingTerm(text: str, term: str) -> bool:
        source = str(text or "").lower()
        target = str(term or "").lower()
        if not source or not target:
            return False
        if re.fullmatch(r"[a-z][a-z ]*", target):
            return re.search(
                rf"(?<![a-z]){re.escape(target)}(?![a-z])",
                source,
            ) is not None
        return target in source

    @staticmethod
    def _IngredientTermAliases(term: str) -> tuple[str, ...]:
        aliases: list[str] = []
        for marker, values in INGREDIENT_TERM_ALIASES:
            if ProductUnderstandingComponent._ContainsTerm(term, marker):
                for value in values:
                    if value not in aliases:
                        aliases.append(value)
        return tuple(aliases)

    @staticmethod
    def _ContainsTerm(text: str, term: str) -> bool:
        return str(term).lower() in str(text).lower()

    @staticmethod
    def _BuildIngredientEntries(
        *,
        productFacts: tuple[dict[str, JsonValue], ...],
        reconstructedTables: tuple[dict[str, JsonValue], ...],
        extractionTraces: list[CompositionExtractionTrace] | None = None,
    ) -> list[dict[str, JsonValue]]:
        entries: list[dict[str, JsonValue]] = []
        for fact in productFacts:
            field = ProductUnderstandingComponent._ReadTextField(fact, "field_name")
            value = ProductUnderstandingComponent._ReadTextField(fact, "normalized_value")
            ProductUnderstandingComponent._ExtendIngredientEntries(
                entries,
                fieldName=field,
                value=value,
                sourceRefs=ProductUnderstandingComponent._ReadSourceRefs(fact),
                sourceKind="product_fact",
                extractionTraces=extractionTraces,
            )

        for table in reconstructedTables:
            rows = table.get("rows")
            tableRefs = ProductUnderstandingComponent._ReadSourceRefs(table)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                field = ProductUnderstandingComponent._ReadTextField(row, "field_name")
                value = ProductUnderstandingComponent._ReadTextField(
                    row,
                    "normalized_value",
                )
                sourceRefs = ProductUnderstandingComponent._ReadSourceRefs(row) or tableRefs
                ProductUnderstandingComponent._ExtendIngredientEntries(
                    entries,
                    fieldName=field,
                    value=value,
                    sourceRefs=sourceRefs,
                    sourceKind="reconstructed_table",
                    extractionTraces=extractionTraces,
                )
        return ProductUnderstandingComponent._DedupIngredientEntries(entries)

    @staticmethod
    def _BuildUserIngredientEntries(
        *,
        userIngredients: tuple[dict[str, JsonValue], ...],
        extractionTraces: list[CompositionExtractionTrace],
    ) -> list[dict[str, JsonValue]]:
        entries: list[dict[str, JsonValue]] = []
        secondaryOrder = 2
        for index, ingredient in enumerate(userIngredients):
            name = str(ingredient.get("name") or "").strip()
            role = str(ingredient.get("role") or "").strip()
            percentage = ingredient.get("percentage")
            if not name or role not in ("primary", "secondary"):
                continue
            if not isinstance(percentage, (int, float)):
                continue
            orderIndex = 1 if role == "primary" else secondaryOrder
            if role == "secondary":
                secondaryOrder += 1
            sourceRef = f"user_input:ingredients:{index}"
            entries.append({
                "ingredient_name": name,
                "order_index": orderIndex,
                "scope": "product",
                "component_name": "",
                "source_field_name": "user_input.ingredients",
                "source_refs": [sourceRef],
                "source_kind": "user_input",
                "source": "user_input",
                "role": role,
                "percent": float(percentage),
            })
            extractionTraces.append(
                CompositionExtractionTrace(
                    sourceFieldName="user_input.ingredients",
                    sourceText=f"{name}: {percentage}%",
                    selectedSpan=name,
                    outputField="ingredient_entries",
                    normalizedValue=name,
                    extractionMethod="user_input",
                    decisionReason="user_declared_ingredient",
                    confidence=1.0,
                    sourceRefs=(sourceRef,),
                ),
            )
        return entries

    @staticmethod
    def _ExtendIngredientEntries(
        entries: list[dict[str, JsonValue]],
        *,
        fieldName: str,
        value: str,
        sourceRefs: tuple[str, ...],
        sourceKind: str,
        extractionTraces: list[CompositionExtractionTrace] | None = None,
    ) -> None:
        if not fieldName or not value:
            return
        if not ProductUnderstandingComponent._IsCompositionFieldName(fieldName):
            return
        componentName = ProductUnderstandingComponent._ReadComponentName(fieldName)
        scope = "component" if componentName else "product"
        for orderIndex, segment in enumerate(
            ProductUnderstandingComponent._SplitTopLevelIngredients(value),
            start=1,
        ):
            ingredientName = ProductUnderstandingComponent._ReadIngredientName(segment)
            if not ingredientName:
                continue
            percentage = ProductUnderstandingComponent._ReadTopLevelPercentage(segment)
            if extractionTraces is not None:
                ProductUnderstandingComponent._AppendExcludedPercentageTraces(
                    extractionTraces,
                    fieldName=fieldName,
                    value=value,
                    segment=segment,
                    sourceRefs=sourceRefs,
                )
                extractionTraces.append(
                    CompositionExtractionTrace(
                        sourceFieldName=fieldName,
                        sourceText=value,
                        selectedSpan=segment,
                        outputField="ingredient_entries",
                        normalizedValue=ingredientName,
                        extractionMethod="regex_top_level_split",
                        decisionReason="composition_field_segment",
                        confidence=0.7,
                        sourceRefs=sourceRefs,
                    ),
                )
                if percentage is not None:
                    extractionTraces.append(
                        CompositionExtractionTrace(
                            sourceFieldName=fieldName,
                            sourceText=value,
                            selectedSpan=segment,
                            outputField="ingredient_percentages",
                            normalizedValue=f"{ingredientName}: {percentage}",
                            extractionMethod="regex_percent",
                            decisionReason="top_level_percent_match",
                            confidence=0.8,
                            sourceRefs=sourceRefs,
                        ),
                    )
            entry: dict[str, JsonValue] = {
                "ingredient_name": ingredientName,
                "order_index": orderIndex,
                "scope": scope,
                "component_name": componentName,
                "source_field_name": fieldName,
                "source_refs": list(sourceRefs),
                "source_kind": sourceKind,
            }
            if percentage is not None:
                entry["percent"] = percentage
            entries.append(entry)

    @staticmethod
    def _AppendNutritionPercentExclusionTraces(
        *,
        reconstructedTables: tuple[dict[str, JsonValue], ...],
        extractionTraces: list[CompositionExtractionTrace],
    ) -> None:
        for table in reconstructedTables:
            tableName = str(table.get("table_name") or "").strip()
            rows = table.get("rows")
            tableRefs = ProductUnderstandingComponent._ReadSourceRefs(table)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                fieldName = ProductUnderstandingComponent._ReadTextField(row, "field_name")
                value = ProductUnderstandingComponent._ReadTextField(row, "normalized_value")
                dailyValuePercent = ProductUnderstandingComponent._ReadTextField(
                    row,
                    "daily_value_percent",
                )
                if not dailyValuePercent:
                    continue
                if not NUTRITION_FIELD_RE.search(f"{tableName} {fieldName} {value}"):
                    continue
                unit = ProductUnderstandingComponent._ReadTextField(row, "unit")
                selectedSpan = " ".join(
                    item
                    for item in (fieldName, value, unit, f"{dailyValuePercent}%")
                    if item
                )
                sourceRefs = ProductUnderstandingComponent._ReadSourceRefs(row) or tableRefs
                extractionTraces.append(
                    CompositionExtractionTrace(
                        sourceFieldName=fieldName,
                        sourceText=selectedSpan,
                        selectedSpan=selectedSpan,
                        outputField="ingredient_percentages",
                        normalizedValue="",
                        extractionMethod="regex_percent_excluded",
                        decisionReason="nutrition_daily_value_percent",
                        confidence=1.0,
                        sourceRefs=sourceRefs,
                    ),
                )

    @staticmethod
    def _AppendExcludedPercentageTraces(
        extractionTraces: list[CompositionExtractionTrace],
        *,
        fieldName: str,
        value: str,
        segment: str,
        sourceRefs: tuple[str, ...],
    ) -> None:
        for match in PERCENT_RE.finditer(segment):
            term = " ".join((match.group("term") or "").split())[-40:].strip(" ,:/")
            reason = ""
            if ProductUnderstandingComponent._IsNestedPercentage(
                segment,
                match.start("percent"),
            ):
                reason = "nested_origin_or_subingredient_percent"
            elif ORIGIN_TERM_RE.search(term):
                reason = "origin_marker_percent"
            if not reason:
                continue
            extractionTraces.append(
                CompositionExtractionTrace(
                    sourceFieldName=fieldName,
                    sourceText=value,
                    selectedSpan=match.group(0).strip(),
                    outputField="ingredient_percentages",
                    normalizedValue="",
                    extractionMethod="regex_percent_excluded",
                    decisionReason=reason,
                    confidence=1.0,
                    sourceRefs=sourceRefs,
                ),
            )

    @staticmethod
    def _SplitTopLevelIngredients(text: str) -> tuple[str, ...]:
        parts: list[str] = []
        current: list[str] = []
        depth = 0
        for character in text:
            if character in "([{":
                depth += 1
            elif character in ")]}":
                depth = max(0, depth - 1)
            if character in ",，、" and depth == 0:
                part = "".join(current).strip()
                if part:
                    parts.append(part)
                current = []
                continue
            current.append(character)
        part = "".join(current).strip()
        if part:
            parts.append(part)
        return tuple(parts)

    @staticmethod
    def _ReadIngredientName(segment: str) -> str:
        percentage = ProductUnderstandingComponent._ReadTopLevelPercentageEntry(segment)
        if percentage is not None:
            return str(percentage.get("term") or "").strip()
        name = re.split(r"[\(\[\{（［]", segment, maxsplit=1)[0]
        name = re.sub(r"\d+(?:[.,]\d+)?\s*%", "", name)
        return " ".join(name.strip(" ,:/·-").split())

    @staticmethod
    def _ReadTopLevelPercentage(segment: str) -> JsonValue | None:
        percentage = ProductUnderstandingComponent._ReadTopLevelPercentageEntry(segment)
        if percentage is None:
            return None
        return percentage.get("percent")

    @staticmethod
    def _ReadTopLevelPercentageEntry(segment: str) -> dict[str, JsonValue] | None:
        percentages = ProductUnderstandingComponent._ExtractIngredientPercentages(segment)
        return percentages[0] if percentages else None

    @staticmethod
    def _DedupIngredientEntries(
        entries: list[dict[str, JsonValue]],
    ) -> list[dict[str, JsonValue]]:
        out: list[dict[str, JsonValue]] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for entry in entries:
            key = (
                str(entry.get("ingredient_name") or "").lower(),
                str(entry.get("scope") or ""),
                str(entry.get("component_name") or ""),
                str(entry.get("order_index") or ""),
                str(entry.get("percent") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(entry)
        return out

    @staticmethod
    def _IngredientPercentagesFromEntries(
        ingredientEntries: list[dict[str, JsonValue]],
    ) -> list[dict[str, JsonValue]]:
        percentages: list[dict[str, JsonValue]] = []
        seen: set[tuple[str, str]] = set()
        for entry in ingredientEntries:
            percent = entry.get("percent")
            if percent is None:
                continue
            term = str(entry.get("ingredient_name") or "").strip()
            key = (term.lower(), str(percent))
            if term and key not in seen:
                item: dict[str, JsonValue] = {"term": term, "percent": percent}
                aliases = ProductUnderstandingComponent._IngredientTermAliases(term)
                if aliases:
                    item["term_aliases"] = list(aliases)
                percentages.append(item)
                seen.add(key)
        return percentages

    @staticmethod
    def _BuildComponentCompositions(
        *,
        productFacts: tuple[dict[str, JsonValue], ...],
        reconstructedTables: tuple[dict[str, JsonValue], ...],
        ingredientEntries: list[dict[str, JsonValue]],
    ) -> list[dict[str, JsonValue]]:
        components: dict[str, dict[str, JsonValue]] = {}
        for entry in ingredientEntries:
            componentName = str(entry.get("component_name") or "").strip()
            if not componentName:
                continue
            component = components.setdefault(
                componentName,
                {
                    "component_name": componentName,
                    "ingredient_names": [],
                    "source_refs": [],
                },
            )
            ingredientNames = component.setdefault("ingredient_names", [])
            if isinstance(ingredientNames, list):
                ingredientName = str(entry.get("ingredient_name") or "").strip()
                if ingredientName and ingredientName not in ingredientNames:
                    ingredientNames.append(ingredientName)
            ProductUnderstandingComponent._MergeSourceRefs(
                component,
                ProductUnderstandingComponent._ReadEntrySourceRefs(entry),
            )

        ProductUnderstandingComponent._ApplyComponentFacts(
            components,
            productFacts,
        )
        ProductUnderstandingComponent._ApplyComponentTableRows(
            components,
            reconstructedTables,
        )
        return [
            components[key]
            for key in sorted(components)
        ]

    @staticmethod
    def _ApplyComponentFacts(
        components: dict[str, dict[str, JsonValue]],
        productFacts: tuple[dict[str, JsonValue], ...],
    ) -> None:
        for fact in productFacts:
            field = ProductUnderstandingComponent._ReadTextField(fact, "field_name")
            value = ProductUnderstandingComponent._ReadTextField(fact, "normalized_value")
            sourceRefs = ProductUnderstandingComponent._ReadSourceRefs(fact)
            componentName = ProductUnderstandingComponent._ReadComponentName(field)
            if componentName:
                component = components.setdefault(
                    componentName,
                    {"component_name": componentName, "ingredient_names": [], "source_refs": []},
                )
                ProductUnderstandingComponent._MergeSourceRefs(component, sourceRefs)
                compactField = field.replace(" ", "")
                if "식품유형" in compactField or "식품의유형" in compactField:
                    component["food_type"] = value
                if "내용량" in compactField or "중량" in compactField:
                    ProductUnderstandingComponent._SetComponentWeight(
                        component,
                        value,
                        "",
                    )
            ProductUnderstandingComponent._ApplyComponentWeightsFromText(
                components,
                value,
                sourceRefs,
            )

    @staticmethod
    def _ApplyComponentTableRows(
        components: dict[str, dict[str, JsonValue]],
        reconstructedTables: tuple[dict[str, JsonValue], ...],
    ) -> None:
        for table in reconstructedTables:
            rows = table.get("rows")
            tableRefs = ProductUnderstandingComponent._ReadSourceRefs(table)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                field = ProductUnderstandingComponent._ReadTextField(row, "field_name")
                value = ProductUnderstandingComponent._ReadTextField(
                    row,
                    "normalized_value",
                )
                unit = ProductUnderstandingComponent._ReadTextField(row, "unit")
                sourceRefs = ProductUnderstandingComponent._ReadSourceRefs(row) or tableRefs
                componentName = ProductUnderstandingComponent._ReadComponentFromContentField(
                    field,
                )
                if componentName:
                    component = components.setdefault(
                        componentName,
                        {
                            "component_name": componentName,
                            "ingredient_names": [],
                            "source_refs": [],
                        },
                    )
                    ProductUnderstandingComponent._SetComponentWeight(
                        component,
                        value,
                        unit,
                    )
                    ProductUnderstandingComponent._MergeSourceRefs(
                        component,
                        sourceRefs,
                    )
                ProductUnderstandingComponent._ApplyComponentWeightsFromText(
                    components,
                    f"{field} {value} {unit}",
                    sourceRefs,
                )

    @staticmethod
    def _ApplyComponentWeightsFromText(
        components: dict[str, dict[str, JsonValue]],
        text: str,
        sourceRefs: tuple[str, ...],
    ) -> None:
        for match in CONTENT_WEIGHT_RE.finditer(text):
            componentName = " ".join((match.group("component") or "").split()).strip(" ,:/")
            amount = ProductUnderstandingComponent._ReadNumericValue(
                match.group("amount"),
            )
            unit = (match.group("unit") or "").lower()
            if not componentName or amount is None:
                continue
            if (
                re.search(r"[A-Za-z가-힣]", componentName) is None
                or componentName.lower() in {"g", "kg", "ml", "l"}
                or ProductUnderstandingComponent._IsComponentWeightNoise(
                    componentName,
                    text,
                )
            ):
                continue
            component = components.setdefault(
                componentName,
                {"component_name": componentName, "ingredient_names": [], "source_refs": []},
            )
            component["content_weight"] = amount
            component["content_weight_unit"] = unit
            ProductUnderstandingComponent._MergeSourceRefs(component, sourceRefs)

    @staticmethod
    def _IsComponentWeightNoise(componentName: str, text: str) -> bool:
        normalizedName = componentName.replace(" ", "")
        if normalizedName in COMPONENT_WEIGHT_NOISE_NAMES:
            return True
        return bool(NUTRITION_FIELD_RE.search(text))

    @staticmethod
    def _SetComponentWeight(
        component: dict[str, JsonValue],
        value: str,
        unit: str,
    ) -> None:
        amount = ProductUnderstandingComponent._ReadNumericValue(value)
        if amount is None:
            return
        component["content_weight"] = amount
        if unit:
            component["content_weight_unit"] = unit

    @staticmethod
    def _BuildPrincipalCandidates(
        ingredientEntries: list[dict[str, JsonValue]],
    ) -> list[dict[str, JsonValue]]:
        productEntries = [
            entry
            for entry in ingredientEntries
            if entry.get("scope") == "product"
            and str(entry.get("ingredient_name") or "").strip()
        ]
        candidatesByName: dict[str, dict[str, JsonValue]] = {}
        if productEntries:
            firstEntry = min(
                productEntries,
                key=lambda entry: int(entry.get("order_index") or 9999),
            )
            ProductUnderstandingComponent._AddPrincipalCandidate(
                candidatesByName,
                firstEntry,
                basis="ingredient_order_first",
                confidence=0.65,
            )
        for entry in productEntries:
            percent = entry.get("percent")
            if not isinstance(percent, (int, float)):
                continue
            confidence = 0.9 if percent >= 15 else 0.35
            ProductUnderstandingComponent._AddPrincipalCandidate(
                candidatesByName,
                entry,
                basis="explicit_percent",
                confidence=confidence,
                percent=percent,
                role="minor_ingredient" if percent < 10 else "major_ingredient",
            )
        if not productEntries:
            for entry in ingredientEntries:
                if entry.get("scope") != "component":
                    continue
                percent = entry.get("percent")
                if not isinstance(percent, (int, float)):
                    continue
                ProductUnderstandingComponent._AddPrincipalCandidate(
                    candidatesByName,
                    entry,
                    basis="component_explicit_percent",
                    confidence=0.45,
                    percent=percent,
                    role="component_ingredient",
                )
        return sorted(
            candidatesByName.values(),
            key=lambda item: (
                -float(item.get("confidence") or 0.0),
                -float(item.get("percent") or 0.0),
                int(item.get("order_index") or 9999),
                str(item.get("ingredient_name") or ""),
            ),
        )

    @staticmethod
    def _AddPrincipalCandidate(
        candidatesByName: dict[str, dict[str, JsonValue]],
        entry: dict[str, JsonValue],
        *,
        basis: str,
        confidence: float,
        percent: float | None = None,
        role: str = "",
    ) -> None:
        ingredientName = str(entry.get("ingredient_name") or "").strip()
        if not ingredientName:
            return
        key = ingredientName.lower()
        candidate = candidatesByName.setdefault(
            key,
            {
                "ingredient_name": ingredientName,
                "confidence": confidence,
                "basis": basis,
                "order_index": entry.get("order_index") or 9999,
                "scope": str(entry.get("scope") or "product"),
                "component_name": str(entry.get("component_name") or ""),
                "source_refs": [],
            },
        )
        candidate["confidence"] = max(
            float(candidate.get("confidence") or 0.0),
            confidence,
        )
        basisText = str(candidate.get("basis") or "")
        if basis not in basisText.split("+"):
            candidate["basis"] = "+".join(filter(None, [basisText, basis]))
        if percent is not None:
            candidate["percent"] = percent
        if role:
            candidate["role"] = role
        ProductUnderstandingComponent._MergeSourceRefs(
            candidate,
            ProductUnderstandingComponent._ReadEntrySourceRefs(entry),
        )

    @staticmethod
    def _PrincipalStatus(candidates: list[dict[str, JsonValue]]) -> str:
        if not candidates:
            return "unknown"
        productCandidates = [
            candidate
            for candidate in candidates
            if str(candidate.get("scope") or "product") == "product"
        ]
        if not productCandidates:
            return "unknown"
        topConfidence = float(productCandidates[0].get("confidence") or 0.0)
        return "confirmed" if topConfidence >= 0.8 else "ambiguous"

    @staticmethod
    def _ReadTextField(value: Mapping[str, JsonValue], key: str) -> str:
        item = value.get(key)
        return str(item or "").strip()

    @staticmethod
    def _ReadSourceRefs(value: Mapping[str, JsonValue]) -> tuple[str, ...]:
        refs = value.get("source_refs")
        if not isinstance(refs, list):
            return ()
        return tuple(str(ref).strip() for ref in refs if str(ref).strip())

    @staticmethod
    def _ReadEntrySourceRefs(entry: Mapping[str, JsonValue]) -> tuple[str, ...]:
        return ProductUnderstandingComponent._ReadSourceRefs(entry)

    @staticmethod
    def _ReadComponentName(fieldName: str) -> str:
        match = COMPONENT_NAME_RE.search(fieldName)
        if match is None:
            return ""
        return " ".join((match.group("component") or "").strip().split())

    @staticmethod
    def _ReadComponentFromContentField(fieldName: str) -> str:
        normalizedFieldName = " ".join(fieldName.split())
        compactFieldName = normalizedFieldName.replace(" ", "")
        if compactFieldName in {"내용량", "총내용량", "중량", "용량", "중량/용량", "중량용량"}:
            return ""
        for marker in ("내용량", "중량", "용량"):
            if marker not in normalizedFieldName:
                continue
            componentName = normalizedFieldName.split(marker, 1)[0].strip(" :：-/")
            if componentName:
                return componentName
        return ProductUnderstandingComponent._ReadComponentName(fieldName)

    @staticmethod
    def _ReadNumericValue(value: str) -> JsonValue | None:
        match = re.search(r"\d+(?:[.,]\d+)?", value or "")
        if match is None:
            return None
        numberText = match.group(0).replace(",", ".")
        try:
            number = float(numberText)
        except ValueError:
            return numberText
        return int(number) if number.is_integer() else number

    @staticmethod
    def _MergeSourceRefs(
        target: dict[str, JsonValue],
        sourceRefs: tuple[str, ...],
    ) -> None:
        current = target.get("source_refs")
        refs = [str(ref) for ref in current] if isinstance(current, list) else []
        for sourceRef in sourceRefs:
            if sourceRef not in refs:
                refs.append(sourceRef)
        target["source_refs"] = refs

    @staticmethod
    def _ExtractIngredientPercentages(text: str) -> list[dict[str, JsonValue]]:
        percentages: list[dict[str, JsonValue]] = []
        seenPercentages: set[tuple[str, str]] = set()
        for match in PERCENT_RE.finditer(text):
            if ProductUnderstandingComponent._IsNestedPercentage(
                text,
                match.start("percent"),
            ):
                continue
            term = " ".join((match.group("term") or "").split())[-40:].strip(" ,:/")
            percentRaw = (match.group("percent") or "").replace(",", ".")
            try:
                percent: JsonValue = float(percentRaw)
            except ValueError:
                percent = percentRaw
            key = (term.lower(), str(percent))
            if term and key not in seenPercentages and not ORIGIN_TERM_RE.search(term):
                percentages.append({"term": term, "percent": percent})
                seenPercentages.add(key)
        return percentages

    @staticmethod
    def _IsProductLevelCompositionFieldName(fieldName: str) -> bool:
        if not ProductUnderstandingComponent._IsCompositionFieldName(fieldName):
            return False
        # 괄호가 붙은 원재료 필드는 구성품 scoped fact다. 함량은 보존하되
        # 전체 상품의 주성분으로 승격하지 않는다.
        return not re.search(r"[\(\[（［].+[\)\]）］]", fieldName)

    @staticmethod
    def _IsCompositionFieldName(fieldName: str) -> bool:
        compactFieldName = fieldName.replace(" ", "").lower()
        if NUTRITION_FIELD_RE.search(compactFieldName):
            return False
        return any(
            marker.lower() in compactFieldName
            for marker in COMPOSITION_PERCENT_FIELD_MARKERS
        )

    @staticmethod
    def _IsNestedPercentage(text: str, index: int) -> bool:
        depth = 0
        for character in text[:index]:
            if character in "([{":
                depth += 1
            elif character in ")]}":
                depth = max(0, depth - 1)
        return depth > 0

    @staticmethod
    def _DedupStrings(values: list[str] | tuple[str, ...], *, limit: int) -> tuple[str, ...]:
        out: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
            if len(out) >= limit:
                break
        return tuple(out)

    @staticmethod
    def _RoutingTerms(
        *,
        productName: str,
        distilledIdentity: DistilledIdentityFacts,
        identity: IdentityHintSet,
    ) -> tuple[str, ...]:
        terms: list[str] = []
        for value in (
            productName,
            identity.commercialIdentity,
            identity.translatedProductName,
            identity.normalizedTariffDescription,
            *identity.productFormTerms,
            *identity.domainHints,
            *identity.chapterHintTerms,
            *identity.chapterHintSourceTerms,
            *distilledIdentity.productFormSignalTerms,
            *distilledIdentity.processingSignalTerms,
            *identity.identityTerms,
        ):
            text = str(value).strip()
            if text and text not in terms:
                terms.append(text)
        return tuple(terms[:80])

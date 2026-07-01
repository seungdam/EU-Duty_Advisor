# ASAP 개발일지: 2026-06-15 이후 파이프라인 재구성

작성자: 김승준  
작성 기준: 2026-07-01  
작업 루트: `/Users/snu/ASAP`  
목적: EU 수출 자동화 파이프라인에서 사용자의 설계 의도, 실제 구현 코드, 검증 결과를 한 문서로 남긴다.

## 1. 프로젝트 목표 재정의

초기 구현은 여러 agent 이름을 붙였지만 실제로는 단방향 함수 호출과 로그 기록에 가까웠다. 사용자가 계속 문제로 지적한 핵심은 다음이었다.

- 블랙보드가 agent 간 검토/감사/수정 공간이 아니라 단순 로그판처럼 보였다.
- 분류기는 CN8/TARIC10을 충분히 설명 가능한 방식으로 좁히지 못했다.
- TARIC10은 LLM이 추론할 대상이 아니라, CN8 확정 후 `taric_master_table`에서 모든 declarable TARIC10 branch를 가져와야 했다.
- 서류 추천은 baseline / pre-TARIC / post-TARIC 구조로 설명되어야 하는데, UI와 agent 사이의 역할이 섞여 있었다.
- ProductUnderstanding이 만드는 상품 설명이 불안정하면 DomainRouter와 Classifier가 모두 틀어진다.

따라서 현재 개발 방향은 다음으로 고정했다.

```text
URL/OCR/COI evidence
  -> EvidenceIntake
  -> ProductUnderstanding: OCR/상품명/사전/LLM 증류로 ProductFacts_dto 생성
  -> DomainRouter: cn_chapter_index를 읽어 chapter 후보 top-k 생성
  -> Classification: ProductFacts_dto + Route_dto를 이용해 HS4 -> HS6 -> CN8 단계 분류
  -> TARIC branch resolver: CN8 아래 모든 declarable TARIC10 branch 열거
  -> DocumentAgent: baseline/pre/post 서류 DTO 구성
  -> Orchestrator: 최종 decision 및 review 상태 정리
```

이 구조에서 LLM은 모든 것을 직접 판단하는 agent가 아니라, 특정 단계에서 닫힌 입력/출력 DTO를 생성하거나 후보 중 선택하는 도구로 제한한다.

## 2. 6월 15일 이후 가장 큰 방향 전환

### 2.1 팀장 classifier를 주 경로로만 두기 어렵다는 판단

기존 팀장 classifier는 `cn_table` 기반 retriever/LLM/validator 흐름을 가지고 있었지만, 프로젝트 목표상 다음 한계가 있었다.

- 분류 중간 단계가 블랙보드에 충분히 남지 않았다.
- 어떤 입력 문장이 어떤 HS4/HS6/CN8 선택에 쓰였는지 추적하기 어려웠다.
- ProductUnderstanding/DomainRouter가 만든 신호가 classifier 단계에서 구조적으로 소비되지 않았다.
- fallback retriever 후보가 실제 제품과 무관하게 넓은 품목으로 튀는 현상이 있었다.

그래서 팀장 classifier는 발표용/비교용/후보 fallback으로 남기되, 사용자가 직접 제어 가능한 staged classifier를 `src/agents/tools/staged_classification.py`에 만들었다.

### 2.2 ProductUnderstanding을 중심에 둔 이유

사용자가 반복해서 제기한 문제는 "OCR과 상품설명에서 이미 최대한 정보를 뽑아도, 이를 관세 분류 용어로 바꾸지 못하면 classifier가 틀린다"는 것이었다.

예시:

- 라면이 `190240`으로 잘못 가는 문제
- 데오도란트가 식품 chapter로 빠지는 문제
- 낙지볶음/꼬막 비빔장처럼 주재료와 조리 형태가 충돌하는 문제
- "다진", "냉동", "손질" 같은 단어가 가공식품으로 과추론되는 문제
- 알레르기 정보가 주성분처럼 들어가 분류를 오염시키는 문제

그래서 `ProductFacts_dto`를 단순 OCR dump가 아니라 다음 두 lane으로 나눴다.

- `identity_lane`: 상품 정체성, 외부 사전/백과 grounding, tariff-like description
- `composition_lane`: 원재료/가공상태/알레르겐 제외 정보
- `classifier_projection`: classifier가 실제로 읽을 compact context

구현 위치:

- `src/agents/dto.py`의 `ProductFacts_dto`
- `src/agents/product_understanding_agent.py`의 `ProductUnderstandingAgent.run`

### 2.3 DomainRouter를 독립 agent로 세운 이유

사용자의 의도는 DomainRouter가 단순 식품/화장품 분기기가 아니라, `ProductFacts_dto`를 읽고 `cn_chapter_index`의 include/exclude/guardrail 성격의 컬럼을 이용해 HS chapter 후보를 만드는 것이었다.

DomainRouter가 필요한 이유:

- classifier가 전체 CN table을 무작정 검색하면 엉뚱한 chapter 후보가 쉽게 살아남는다.
- chapter가 틀리면 baseline/pre-TARIC 서류도 같이 틀어진다.
- pre-TARIC은 TARIC10 확정 이후가 아니라 chapter/domain 후보 단계에서 먼저 준비되어야 한다.
- 사용자 의도상 DomainRouter는 classifier와 document pipeline 양쪽에 fan-out하는 gate 역할이다.

구현 위치:

- `src/agents/domain_router_agent.py`
- `src/agents/tools/domain_router.py`
- `src/agents/dto.py`의 `Route_dto`

`Route_dto`는 두 lane을 가진다.

- `classifier_lane`: classifier가 볼 chapter 후보, query terms, negative terms
- `document_lane`: baseline/pre-TARIC document lookup이 볼 chapter/domain/pre-gate 정보

### 2.4 TARIC10은 추론하지 않고 전부 열거하는 방향

사용자는 TARIC10 추천을 LLM이 직접 고르는 방식보다, CN8이 정해졌다면 그 아래 declarable TARIC10을 전부 가져오는 방식이 더 타당하다고 판단했다.

이유:

- TARIC10 뒤 2자리는 LLM이 hallucination하기 쉽다.
- 실제 EU TARIC master table에 존재하지 않는 10자리를 만들어내면 document package가 무의미해진다.
- 각 TARIC10 branch마다 서류 패키지를 만들면 사용자가 실제 차이를 비교할 수 있다.

구현 위치:

- `src/agents/tools/taric_branch_resolver.py`
- `ClassificationAgent._resolve_taric_branches`
- `DocumentAgent._taric_targets_for_candidate`

현재 동작:

- `taric_master_table`에서 `cn8 = %s`로 모든 row를 읽는다.
- `goods_code_10` 기준으로 branch를 묶는다.
- `is_declarable_leaf`가 있으면 non-declarable branch는 document target에서 제외한다.
- candidate 카드에는 primary TARIC10만 보일 수 있지만, `taric10_branch_candidates`에는 모든 branch가 남는다.

## 3. 현재 runtime pipeline

### 3.1 전체 실행 순서

구현 위치: `src/agents/document_pipeline.py`

```python
agents = [
    EvidenceIntakeAgent(raw_input),
    ProductUnderstandingAgent(),
    DomainRouterAgent(),
    ClassificationAgent(),
    DocumentAgent(include_celex_excerpt=include_celex_excerpt),
    OrchestratorAgent(),
]
```

각 agent는 `BaseAgent.execute()`로 실행되고, blackboard와 `agent_runs.jsonl`에 다음을 남긴다.

- 읽은 input id
- 쓴 output id
- citations
- reasoning_summary
- prompt_excerpt
- success/error

### 3.2 EvidenceIntake

구현 위치:

- `src/agents/evidence_intake_agent.py`
- URL/OCR 수집은 `src/agents/document_pipeline.py`와 `src/bussiness_logic/product/*` 쪽이 담당

의도:

- 입력 URL, OCR text, 상품명, 상품설명, 원재료, 상품고시 정보 등을 `ProductEvidenceState`로 정리한다.
- 이 단계에서는 분류하지 않는다.
- downstream agent가 raw OCR을 직접 뒤지지 않게 기본 evidence 객체를 만든다.

출력:

- blackboard slot: `product_evidence_state`
- object_type: `ProductEvidenceState`

한계:

- 현재 Kurly/KurlyGlobal 페이지 OCR 품질과 이미지 구조에 영향을 많이 받는다.
- COI는 입력 형태가 다양해 아직 주 경로로 강하게 연결하지 않았다.

### 3.3 ProductUnderstanding

구현 위치:

- `src/agents/product_understanding_agent.py`
- `src/agents/tools/encyclopedia_lookup.py`
- `src/agents/tools/identity_distiller.py`
- `src/agents/dto.py`의 `ProductFacts_dto`

사용자 의도:

- OCR과 상품명을 관세 분류에 가까운 표현으로 재구성한다.
- 알레르겐/교차오염 문구는 분류 근거에서 제외한다.
- Naver 사전/백과는 raw 결과를 그대로 routing에 넣지 않고, Gemini가 compact identity로 증류한 결과만 쓴다.
- DTO는 길게 늘어지는 raw evidence와 classifier용 compact projection을 분리한다.

현재 구현:

1. 상품명/설명/OCR fact를 모은다.
2. 알레르겐 notice를 `_is_allergen_notice_text`로 제외한다.
3. `build_product_understanding_dto()`가 Gemini로 JSON ProductUnderstanding을 생성한다.
4. Naver encyclopedia lookup을 수행한다.
5. `IdentityDistillerTool`이 Gemini로 raw dictionary/encyclopedia entry를 compact identity로 증류한다.
6. unsupported/hallucinated terms를 source evidence 기준으로 정리한다.
7. `ProductFacts_dto`를 blackboard에 저장한다.

현재 ProductUnderstanding prompt 위치:

- `src/agents/product_understanding_agent.py`의 `_PRODUCT_UNDERSTANDING_SYSTEM_PROMPT`
- `src/agents/product_understanding_agent.py`의 `_TRANSLATION_SYSTEM_PROMPT`
- `src/agents/tools/identity_distiller.py`의 `IdentityDistillerTool._build_prompt`

2026-07-01 수정:

- Gemini bridge adapter가 `finish_reason=length`로 JSON을 중간에서 자르는 문제가 있었다.
- ProductUnderstanding Gemini 경로만 `llm_classifier.llm()` direct caller를 사용하도록 우회했다.
- `ASAP_PRODUCT_UNDERSTANDING_MAX_TOKENS` 기본값을 4096으로 올렸다.
- `llm_understanding_trace`를 `ProductFacts_dto.evidence`에 저장하도록 했다.

검증 결과:

- 샘플 `[압구정낙지] 낙지 볶음 500g`에서 `product_understanding_mode=llm_json`으로 정상 생성됨.
- `normalized_tariff_description = Prepared frozen stir-fried octopus dish`
- `principal_ingredient_terms = octopus, mollusc, vegetables...`
- `chapter_routing_terms`에 prepared seafood / octopus preparation / frozen prepared food 등이 생성됨.

### 3.4 DomainRouter

구현 위치:

- `src/agents/domain_router_agent.py`
- `src/agents/tools/domain_router.py`
- `data/ASAP_Ontology/tables/CN_Chapter_Index.md`
- Supabase table: `cn_chapter_index`, `domain_scope_routes`

사용자 의도:

- ProductUnderstanding이 만든 DTO를 읽어 chapter 후보를 top-k로 만든다.
- classifier에 chapter hint를 제공한다.
- 동시에 baseline/pre-TARIC document lookup에 필요한 chapter/domain/pre_gate 정보를 넘긴다.
- WCO 9-group 기반 확장 구조를 염두에 두되, 데모에서는 식품/화장품 관련 chapter를 우선 안정화한다.

현재 구현:

- `DomainRouterAgent.run()`이 `ProductUnderstandingFacts`와 observed facts를 읽는다.
- `DomainRouterTool.route_product()`가 table-driven으로 chapter 후보를 계산한다.
- `Route_dto`를 만든다.
- `classifier_lane`에는 allowed chapter, query terms, negative terms를 넣는다.
- `document_lane`에는 baseline policy, baseline groups, pre-TARIC scope를 넣는다.

중요한 설계 판단:

- DomainRouter는 document package를 만들지 않는다.
- DomainRouter는 "어느 table/agent로 보내야 하는가"를 결정하는 router이다.
- baseline/pre-TARIC document lookup은 DocumentAgent 쪽 책임이다.

### 3.5 Classification

구현 위치:

- `src/agents/classification_agent.py`
- `src/agents/tools/staged_classification.py`
- `src/agents/llm_classifier.py`
- `src/agents/dto.py`의 `HS4Classify_dto`, `HS6Classify_dto`, `CN8Classify_dto`, `CodeSet_dto`

사용자 의도:

- ProductFacts_dto와 Route_dto를 classifier가 실제로 읽어야 한다.
- 한 번에 CN8을 고르는 것이 아니라 HS4 -> HS6 -> CN8로 점점 좁혀야 한다.
- 각 단계에서 어떤 후보를 보고 왜 남겼는지 blackboard에 남겨야 한다.
- LLM은 전체 classification을 자유롭게 생성하는 것이 아니라, 이미 잘라낸 후보 목록 중에서 code를 고르는 selector로 제한한다.

현재 구현:

- `StagedClassificationTool.classify()`가 `ProductFacts_dto`와 `Route_dto`를 받는다.
- `llm_classifier._load_cn_rows()`로 Supabase `cn_table`을 읽는다.
- DomainRouter의 chapter 후보를 기반으로 HS4 후보 pool을 만든다.
- 살아남은 HS4 기준으로 HS6 후보 pool을 만든다.
- 살아남은 HS6 기준으로 CN8 후보 pool을 만든다.
- 각 stage는 `HS4Classify_dto`, `HS6Classify_dto`, `CN8Classify_dto`로 blackboard에 저장된다.

2026-07-01 수정:

- 기존에는 staged classifier가 실제 단계 narrowing이 아니라 한 CN8 결과를 projection하는 수준이었다.
- `src/agents/tools/staged_classification.py`를 고쳐 각 stage별 candidate slice를 따로 만들게 했다.
- ProductFacts에서 읽는 context를 `classifier_projection` 중심으로 축소했다.
- stage prompt를 짧게 만들고, 응답은 `{"codes":[...]}`만 받게 했다.
- Gemini가 JSON을 일부 자르는 경우 raw response에 candidate code가 보이면 salvage하여 fallback을 피하도록 했다.

샘플 검증:

`[압구정낙지] 낙지 볶음 500g`

```text
HS4 selected: 1605, 1602, 2103
HS6 selected: 160555
CN8 selected: 16055500
top TARIC10: 1605550000
expected HS6: 160555
match: true
```

현재 prompt 위치:

- `src/agents/tools/staged_classification.py`의 `StagedClassificationTool._select_stage`
- 내부 LLM caller: `src/agents/llm_classifier.py`의 `llm()`

### 3.5.1 classifier 관련 파일이 많아진 이유와 각 파일의 기능

현재 `src/agents` 안에는 classifier라는 이름이 붙은 파일과 함수가 많다. 이것은 하나의 classifier를 중복 구현한 것이 아니라, 서로 다른 목적의 분류 경로를 같은 blackboard contract(`CandidateCodeSet`, `ClassificationStageResult`)로 맞추는 과정에서 생긴 구조다.

사용자의 의도는 다음이었다.

- 팀장이 만든 Stage 1 classifier는 발표/비교/호환 경로로 유지한다.
- 사용자가 만든 LLM 기반 실험 경로는 `ProductFacts_dto`와 `Route_dto`를 실제로 읽는 방향으로 강화한다.
- 두 경로 모두 최종적으로는 같은 `CandidateCodeSet`을 써서 TARIC/document 단계가 분류기 구현에 의존하지 않게 한다.
- TARIC10은 classifier가 추론하지 않고, CN8 이후 deterministic resolver가 `taric_master_table`에서 전부 열거한다.

그래서 classifier 계열은 다음처럼 나뉜다.

| 파일/컴포넌트 | 현재 역할 | 왜 필요한가 | 최종 산출물 |
| --- | --- | --- | --- |
| `src/agents/classification_agent.py` | blackboard 상의 공식 `Classification_Agent` | ProductFacts/Route를 읽고, staged classifier 또는 팀장 classifier/fallback을 실행하며, 결과를 `CandidateCodeSet`으로 표준화한다 | `ClassificationStageResult`, `CandidateCodeSet` |
| `src/agents/tools/staged_classification.py` | 사용자가 설계한 실험용 staged classifier tool | HS4 -> HS6 -> CN8을 단계별로 좁히고, 각 단계에서 어떤 후보를 보고 남겼는지 audit 가능하게 만든다 | HS4/HS6/CN8 stage payload |
| `src/agents/llm_classifier.py` | Supabase `cn_table` loader + Gemini/Ollama caller + legacy LLM classifier | staged classifier가 후보 slice를 고른 뒤 LLM selector를 호출할 때 재사용한다. 예전 단일 LLM classifier fallback도 여기에 남아 있다 | classifier rows 또는 LLM raw response |
| `src/agents/_external_classifier.py` | 팀장 Stage 1 classifier adapter | `src/bussiness_logic`의 팀장 분류 파이프라인 입력/출력을 ASAP blackboard 형태로 맞춘다 | `ExternalClassificationResult` |
| `src/bussiness_logic/core/classification/stage1.py` | 팀장 쪽 원본 Stage 1 classifier 본체 | retriever/context/request/validator/decision/traversal/recommendation 흐름을 가진다. 되도록 수정하지 않는 호환 경로다 | Stage1 recommendation/decision payload |
| `src/agents/tools/taric_branch_resolver.py` | CN8 -> TARIC10 branch resolver | 이름은 classifier 이후 단계지만, 분류가 아니라 deterministic TARIC branch lookup이다. CN8 아래 declarable TARIC10을 전부 가져온다 | `taric10_branch_candidates` |
| `src/agents/dto.py` | classification DTO builder | HS4/HS6/CN8 단계 결과와 최종 CodeSet의 shape를 고정한다 | `HS4Classify_dto`, `HS6Classify_dto`, `CN8Classify_dto`, `CodeSet_dto` |

현재 runtime 우선순위는 다음이다.

```text
ClassificationAgent
  -> StagedClassificationTool
       -> llm_classifier._load_cn_rows()  # Supabase cn_table
       -> llm_classifier.llm()            # Gemini/Ollama selector
       -> HS4/HS6/CN8 stage payload
  -> CandidateCodeSet
  -> TaricBranchResolverTool
       -> Supabase taric_master_table
       -> all declarable TARIC10 branch candidates
```

staged path가 실패하면 호환 경로로 넘어간다.

```text
ClassificationAgent
  -> _external_classifier.run_external_classifier()
       -> bussiness_logic/core/classification/stage1.py
  -> CandidateCodeSet
```

그리고 팀장 retriever가 후보를 못 만들거나 error가 나면 마지막 fallback으로 `llm_classifier.classify()`가 직접 사용될 수 있다. 즉 파일이 많은 이유는 다음 세 경로가 동시에 살아 있기 때문이다.

```text
1. 현재 실험 주 경로: staged_llm_classifier
2. 팀장 호환 경로: external Stage 1 classifier
3. 최후 fallback/비교 경로: llm_classifier direct classify
```

향후 정리 방향은 `ClassificationAgent`를 공식 entrypoint로 유지하고, `staged_classification.py`를 사용자의 주 경로로 승격하는 것이다. `_external_classifier.py`와 `bussiness_logic`은 팀장 classifier 호환/비교 경로로 명확히 라벨링하고, `llm_classifier.py`는 LLM caller + Supabase CN loader 역할과 legacy direct classifier 역할을 분리하는 것이 좋다.

### 3.6 TARIC branch resolution

구현 위치:

- `src/agents/tools/taric_branch_resolver.py`
- `src/agents/classification_agent.py`

사용자 의도:

- CN8이 나오면 TARIC10은 추론하지 않는다.
- `taric_master_table`에서 CN8 아래의 모든 declarable TARIC10 branch를 가져온다.
- document package는 각 TARIC10 branch별로 만들 수 있어야 한다.

현재 구현:

- Supabase `taric_master_table`에서 `SELECT * FROM taric_master_table WHERE cn8 = %s`
- `goods_code_10` 기준으로 branch group 생성
- declarable leaf 우선 정렬
- candidate에는 `taric10_branch_candidates` 저장
- DocumentAgent가 branch별 target을 순회한다.

### 3.7 DocumentAgent / document package

구현 위치:

- `src/agents/document_agent.py`
- `src/agents/document_package.py`
- `src/agents/tools/document_package_tool.py`
- `src/agents/dto.py`의 `DocBaseSet_dto`, `DocPreSet_dto`, `DocPostSet_dto`

사용자 의도:

- 기본 제출서류는 모든 수출에 필요한 baseline shell로 봐야 한다.
- COA/COI/Health Certificate/CHED 등은 baseline에 무조건 박으면 안 된다.
- 이들은 baseline document shell + pre-TARIC trigger + post-TARIC trigger + product fact를 조합해 판단해야 한다.
- UI는 최종적으로 document binding 하나를 보고, 상세 클릭 시 baseline/pre/post 근거를 보여주는 방향이 맞다.

현재 구현:

- `DocumentPackageTool.resolve()`가 `document_package.get_document_package()`를 호출한다.
- `document_package.py`는 Supabase에서 다음 table을 읽는다.
  - `taric_master_table`
  - `baseline_document_master`
  - `document_binding`
  - `pre_taric_requirement_master`
  - `post_taric_requirement_master`
  - `taric_certificate_declaration_guidance`
  - `taric_celex_table`
- `DocumentAgent._build_document_view()`가 raw package를 agent-owned view model로 바꾼다.
- baseline/pre/post logical DTO set을 만든다.

DTO 역할:

- `DocBaseSet_dto`: baseline document shell과 document binding cards
- `DocPreSet_dto`: chapter/domain/pre-gate 단계에서 필요한 screening checks
- `DocPostSet_dto`: TARIC measure/certificate/legal base로 trigger된 post-TARIC requirements

중요한 판단:

- DocumentAgent는 deterministic이다.
- 현재는 LLM으로 CELEX 해석을 하지 않는다.
- 서류 정확도는 향후 ROSA 기준으로 검증한다.

### 3.8 Orchestrator

구현 위치:

- `src/agents/orchestrator_agent.py`

역할:

- candidate/document/backtracking/missing facts를 보고 최종 `OrchestratorDecision`을 만든다.
- 현재는 multi-agent deliberation보다는 pipeline control과 review 상태 정리에 가깝다.

사용자 판단:

- 완전한 AI blackboard 협업 시스템까지 만들기보다, 현재 프로젝트 목적에서는 agent control과 audit trail로 쓰는 정도가 현실적이다.
- blackboard는 "여러 agent가 같은 product object를 보고 자기 툴콜 결과를 남기는 판"으로 정의한다.

## 4. Supabase-first 전환

사용자 의도:

- 로컬 CSV와 DB가 섞이면 협업과 배포가 불가능하다.
- 데모/팀 작업 기준은 Supabase table로 고정해야 한다.

현재 핵심 table:

- `cn_table`
- `cn_chapter_index`
- `domain_scope_routes`
- `taric_master_table`
- `baseline_document_master`
- `document_binding`
- `pre_taric_requirement_master`
- `post_taric_requirement_master`
- `taric_celex_table`
- `taric_certificate_declaration_guidance`
- `bti_case`

검증 흔적:

- 풀 테스트 로그에서 `[engine] cn_table loaded from supabase rows=9762` 확인
- `TaricBranchResolverTool`도 `_connect_db()`로 Supabase `taric_master_table`을 직접 읽는다.

남은 주의:

- 로컬 CSV fallback이 남아 있는 부분은 계속 제거/격리해야 한다.
- `.env`의 Supabase 연결 값은 외부 공유 금지.

## 5. Ontology / 문서 정규화 작업 의도

사용자는 ontology를 "LLM에게 긴 문서를 읽히는 목적"이 아니라 runtime contract와 table/tool 연결을 명확히 하는 문서로 재정의했다.

현재 ontology 위치:

- `data/ASAP_Ontology/stage_contract`
- `data/ASAP_Ontology/agent_contract`
- `data/ASAP_Ontology/runtime_contract`
- `data/ASAP_Ontology/tables`
- `data/ASAP_Ontology/profiles`

핵심 방향:

- DTO는 runtime에서 agent 간 handoff 형태를 고정한다.
- Ontology는 각 agent가 어떤 table/profile/stage_contract를 읽어야 하는지 설명한다.
- 실제 runtime 강제는 code-driven rule과 DTO validation으로 간다.
- LLM prompt는 최후의 보루가 아니라, code-driven boundary 안에서만 작동해야 한다.

현재 ProductUnderstanding은 ontology 일부를 context chunk로 읽는다.

사용 파일:

- `stage_contract/Product_Understanding.md`
- `stage_contract/Regulatory_Domain_Routing.md`
- `profiles/ProductUnderstandingTool_Profile.md`
- `profiles/DomainRouterTool_Profile.md`
- `tables/CN_Chapter_Index.md`

구현 위치:

- `src/agents/product_understanding_agent.py`의 `_ONTOLOGY_CONTEXT_FILES`
- `_load_ontology_context()`

## 6. Naver encyclopedia / Gemini distillation 도입

사용자 문제의식:

- OCR/상품명만으로는 "재첩", "멘보샤", "명란" 같은 품목 정체성을 안정적으로 관세 용어로 바꾸기 어렵다.
- Naver 검색 raw 결과는 레시피/맛집/광고가 섞여 dirty context가 된다.
- raw search result를 그대로 DTO에 넣으면 DomainRouter가 오염된다.

결정한 방식:

```text
product_name
  -> head noun/query 후보 추출
  -> Naver encyclopedia/dictionary lookup
  -> Gemini IdentityDistillerTool
  -> compact identity만 ProductFacts_dto.identity_lane에 반영
  -> raw Naver entry는 evidence로만 저장, routing projection에서는 제외
```

구현 위치:

- `src/agents/tools/encyclopedia_lookup.py`
- `src/agents/tools/identity_distiller.py`
- `src/agents/product_understanding_agent.py`

중요한 규칙:

- Naver raw description은 routing에 직접 쓰지 않는다.
- Gemini distiller 출력은 closed enum과 compact phrase 중심이다.
- `ingredient_class`, `food_form`, `processing_state`, `normalized_tariff_description`을 만든다.
- raw result는 audit/evidence로 남긴다.

검증 결과:

낙지볶음 샘플에서 IdentityDistiller는 다음을 정상 생성했다.

```json
{
  "normalized_tariff_description": "stir-fried octopus with vegetables and spicy seasoning",
  "ingredient_class": "mollusc",
  "food_form": "mixed_meal",
  "processing_state": "fried",
  "confidence": 0.95
}
```

## 7. 테스트/감사 체계

사용자 요청:

- A/B 비교보다 지금은 LLM 추론 파이프라인 자체를 강화하는 것이 우선이다.
- 테스트 과정에서 ProductFacts 생성 근거, 만들어진 ProductFacts, classification이 ProductFacts를 어떻게 읽었는지, 모든 DTO와 blackboard 산출물을 파일로 보고 싶다.

구현 위치:

- `scripts/pipeline_audit_test.py`

출력 구조:

```text
artifacts/pipeline-audit/<timestamp>/
  report.md
  summary.csv
  summary.json
  summary.partial.jsonl
  rows/
    row_001/
      00_index.md
      input_row.json
      blackboard.json
      agent_runs.jsonl
      product_evidence_state.json
      productfacts_dto.json
      productfacts_generation.json
      productfacts_generation.md
      route_dto.json
      classification_stage_results.json
      classification_usage.json
      classification_usage.md
      prompts.json
      prompts.md
      codeset_dto.json
      document_packages.json
      decision.json
      consistency.json
```

평가 기준:

- 현재 `test/answer.csv`는 미국 HS Code 기반 정답이므로 HS6까지만 accuracy를 본다.
- CN8/TARIC suffix는 평가 대상에서 제외한다.

2026-07-01 수정:

- Excel/CSV에서 `0710807060` 같은 leading zero가 `710807060`으로 깨지는 문제가 있었다.
- `_normalize_expected_code()`를 추가해 7-9자리 answer code는 `zfill(10)` 처리한다.
- 예: `710807060` -> `0710807060` -> HS6 `071080`

현재 실행 중인 풀 테스트:

- output dir: `/Users/snu/ASAP/artifacts/pipeline-audit/20260701_130749`
- dataset: `/Users/snu/ASAP/test/answer.csv`
- selected rows: 46
- 진행 중간 상태는 `summary.partial.jsonl`에 즉시 저장된다.

## 8. 현재까지 확인된 결과

### 8.1 1샘플 smoke

샘플:

- URL: `https://www.kurlyglobal.com/products/m00000188980`
- 제품: `[압구정낙지] 낙지 볶음 500g`
- 기대 HS6: `160555`

결과:

- ProductUnderstanding: `llm_json`
- HS4: `1605`
- HS6: `160555`
- CN8: `16055500`
- TARIC10: `1605550000`
- HS6 match: true
- Supabase `cn_table` read 확인
- document package 생성 확인
- orchestrator decision 생성 확인

### 8.2 풀 테스트 중간에서 확인된 실패 유형

1. Row 2: 기대 `160555`, top `190230`
   - 제품명은 URL로만 표시되어 ProductUnderstanding이 주정체성을 충분히 잡지 못했을 가능성이 있다.
   - row별 산출물: `/Users/snu/ASAP/artifacts/pipeline-audit/20260701_130749/rows/row_002`

2. 이전 실행 row 4: 꼬막 비빔장 기대 `210690`, top `160556`
   - "mollusc ingredient"가 "sauce/seasoning/paste preparation"보다 강하게 작동했다.
   - 향후 classifier decision axis에 "essential character is sauce/condiment vs mollusc preparation" 판별 축이 필요하다.

3. row 6 leading zero issue
   - pipeline 문제가 아니라 answer normalization 문제였다.
   - `_normalize_expected_code()`로 수정했다.

## 9. 지금 코드가 사용자의 의도를 반영한 방식

### 9.1 "agent처럼 보이게"가 아니라 "agent가 남기는 산출물"을 고정

DTO를 `src/agents/dto.py`로 모은 이유는 다음이다.

- agent가 뭘 읽고 뭘 쓰는지 명시적으로 보이게 한다.
- blackboard가 단순 로그가 아니라 handoff object 저장소가 되게 한다.
- UI/admin/smoke test가 같은 산출물을 볼 수 있게 한다.
- 아직 Pydantic/LinkML을 강제하지 않고도 협업 기준을 만든다.

현재 DTO:

- `ProductFacts_dto`
- `Route_dto`
- `HS4Classify_dto`
- `HS6Classify_dto`
- `CN8Classify_dto`
- `CodeSet_dto`
- `DocBaseSet_dto`
- `DocPreSet_dto`
- `DocPostSet_dto`
- `DocBindSet_dto`
- `Decision`

### 9.2 LLM의 역할을 줄이고 감싸기

사용자 의도는 LLM이 마음대로 HS code를 만들어내는 것이 아니었다. 현재 LLM 사용 위치는 제한되어 있다.

1. ProductUnderstanding JSON 생성
   - OCR/상품명을 ProductFacts_dto 재료로 구조화한다.
   - 출력은 정해진 JSON keys로 제한한다.

2. IdentityDistiller
   - Naver raw entry를 compact commodity identity로 증류한다.
   - raw dirty context는 routing에 직접 들어가지 않는다.

3. StagedClassification selector
   - 이미 table에서 잘라낸 후보 중 code만 선택한다.
   - HS4/HS6/CN8 단계별 prompt와 raw response를 저장한다.

LLM이 하지 않는 것:

- TARIC10 생성
- document requirement 생성
- Supabase table에 없는 서류 invent
- 최종 legal determination

### 9.3 baseline/pre/post document 구조

사용자 의도:

- baseline은 모든 수출에서 필요한 document shell이다.
- pre-TARIC은 chapter/domain/pre-gate 기반 screening이다.
- post-TARIC은 TARIC10 measure/certificate/legal basis 기반 requirement이다.
- document_binding은 UI가 볼 최종 binding 관계의 기반이다.

현재 구현:

- `document_package.py`가 table을 읽어 raw package와 checklist summary를 만든다.
- `DocumentAgent._build_document_view()`가 `DocBaseSet_dto`, `DocPreSet_dto`, `DocPostSet_dto`를 만든다.
- UI 최신화가 끝나면 `document_view.dto_sets` 또는 binding 기반 view를 읽는 방향으로 가야 한다.

## 10. 남은 문제와 다음 작업

### 10.1 Classification accuracy 개선

현재 staged classifier 구조는 살아났지만, 정답률 개선을 위해 다음이 필요하다.

- HS4/HS6/CN8 stage별 decision axis를 강화한다.
- 특히 "소스/조미료/비빔장"과 "주재료 준비품" 경계를 분리해야 한다.
- ProductFacts_dto에서 `essential_character_basis` 같은 축을 추가할지 검토한다.
- BTI case를 CN8/TARIC 후보 검증 evidence로 붙인다.

### 10.2 ProductUnderstanding DTO 안정화

현재 DTO는 작동하지만 다음 문제가 남아 있다.

- Gemini 응답이 길면 JSON truncation이 발생할 수 있다.
- `ASAP_PRODUCT_UNDERSTANDING_MAX_TOKENS=4096`으로 완화했다.
- 더 근본적으로는 ProductUnderstanding output schema를 더 compact하게 줄이는 것이 좋다.

### 10.3 Ontology와 runtime의 실제 연결

현재 ontology는 일부만 runtime context로 읽힌다.

다음 단계:

- `ProductUnderstanding_Runtime_Rules.md`를 code-driven rule source로 바꿀지 결정
- DomainRouter가 `CN_Chapter_Index.md`와 Supabase `cn_chapter_index`를 어떻게 매핑하는지 명시
- staged classifier가 stage contract를 prompt/DTO 생성에 어떻게 반영하는지 정리
- LinkML은 후순위로 두되, DTO shape가 안정되면 schema로 내린다.

### 10.4 Blackboard를 control board로 확장

현재 blackboard는 산출물 저장과 감사에는 충분하지만, agent 간 challenge/revision loop는 아직 약하다.

후속 후보:

- Classification challenge producer
- Document mismatch challenge
- Orchestrator UserQuestion producer
- candidate/document conflict를 blackboard에 open issue로 남기기

단, 현재 프로젝트 목적상 이것은 핵심 정확도 개선 이후가 맞다.

## 11. 주요 파일 요약

| 파일 | 역할 | 사용자의 의도와 연결 |
| --- | --- | --- |
| `src/agents/document_pipeline.py` | 전체 pipeline 순서 실행 | 단방향 실행이지만 blackboard 산출물 기준으로 agent handoff를 만든다 |
| `src/agents/dto.py` | DTO builder 모음 | agent 간 주고받는 대답 형태를 명시한다 |
| `src/agents/product_understanding_agent.py` | OCR/상품명 -> ProductFacts_dto | 분류 전 상품 정체성/원재료/가공상태를 관세 용어로 정리한다 |
| `src/agents/tools/encyclopedia_lookup.py` | Naver lookup | obscure Korean commodity를 grounding한다 |
| `src/agents/tools/identity_distiller.py` | Naver raw -> compact identity | dirty context를 직접 routing에 넣지 않는다 |
| `src/agents/domain_router_agent.py` | ProductFacts -> Route_dto | chapter 후보와 document gate를 만든다 |
| `src/agents/tools/domain_router.py` | cn_chapter_index/domain_scope_routes lookup | table-driven routing |
| `src/agents/classification_agent.py` | classification orchestration | staged classifier 또는 fallback classifier를 실행하고 CodeSet을 만든다 |
| `src/agents/tools/staged_classification.py` | HS4 -> HS6 -> CN8 staged classifier | ProductFacts/Route를 실제로 읽어 단계별 후보를 좁힌다 |
| `src/agents/llm_classifier.py` | Supabase `cn_table` loader와 Gemini/Ollama caller | staged classifier의 LLM caller와 cn_table access 기반 |
| `src/agents/tools/taric_branch_resolver.py` | CN8 -> TARIC10 branches | TARIC10 hallucination 방지, 모든 branch 열거 |
| `src/agents/document_package.py` | Supabase document package resolver | baseline/pre/post/TARIC/CELEX/guidance table lookup |
| `src/agents/document_agent.py` | raw package -> document view/DTO | baseline/pre/post를 agent-owned result로 만든다 |
| `scripts/pipeline_audit_test.py` | full pipeline audit test | ProductFacts/classification/blackboard/prompt를 row별 저장 |

## 12. 현재 테스트 산출물 보는 법

풀 테스트 결과:

```text
/Users/snu/ASAP/artifacts/pipeline-audit/20260701_130749
```

주요 파일:

```text
report.md                  # 전체 정확도 요약
summary.csv                # 행별 expected/top/match
summary.partial.jsonl      # 실행 중에도 누적 저장
rows/row_###/00_index.md   # 각 샘플 요약
rows/row_###/blackboard.json
rows/row_###/productfacts_generation.md
rows/row_###/classification_usage.md
rows/row_###/prompts.md
```

한 row에서 사용자가 특히 볼 파일:

```text
productfacts_generation.json
  -> ProductFacts_dto가 어떤 input/prompt/raw response/evidence로 만들어졌는지

classification_usage.json
  -> classifier가 ProductFacts_dto/Route_dto의 어떤 필드를 읽었는지

classification_stage_results.json
  -> HS4/HS6/CN8 각 단계 후보, 선택 코드, prompt, raw response

blackboard.json
  -> 전체 agent 산출물
```

## 13. 결론

6월 15일 이후 작업의 핵심은 "agent 이름을 붙인 함수 묶음"에서 "DTO와 blackboard 산출물로 검증 가능한 pipeline"으로 옮기는 것이었다.

현재까지 달성한 것:

- Supabase-first table read 경로 정리
- ProductFacts_dto / Route_dto / staged classification DTO 도입
- ProductUnderstanding에 Gemini + Naver identity distillation 연결
- DomainRouter를 classifier/document fan-out gate로 정리
- HS4 -> HS6 -> CN8 staged classifier 구현
- CN8 -> 모든 TARIC10 branch 열거
- baseline/pre/post document DTO 구조 추가
- row별 full audit artifact 생성

아직 미완인 것:

- 전체 HS6 정확도 안정화
- sauce/condiment vs animal/mollusc preparation 같은 decision axis 강화
- ontology 문서를 runtime rule source로 더 강하게 연결
- UI가 `document_view.dto_sets`/document binding을 완전히 읽도록 최신화
- blackboard challenge/revision loop 고도화

현재 방향은 사용자의 원래 프로젝트 목표와 일치한다. 즉, EU 수출 자동화에서 "상품 이해 -> chapter routing -> HS/CN/TARIC 분류 -> baseline/pre/post 서류 패키지 -> audit 가능한 blackboard"를 한 흐름으로 만드는 것이다.

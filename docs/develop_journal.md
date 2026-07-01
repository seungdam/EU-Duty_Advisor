# ASAP 개발 일지 (Development Journal) — 2026-06-15 ~ 2026-07-01

작성자: 김승준

> 작성 관점: **개발자(사용자)의 의도 중심.** 각 항목은 *왜 이걸 파이프라인에 넣으려 했나(의도) →
> 어떤 방법/코드로 넣었나(방법) → 결과가 어떻게 나왔나(결과)* 로 정리.
> 협업 형태: AI(Claude)와 상호작용하며 설계·구현·검증을 반복.

---

## 0. 관통하는 설계 원칙 (일지 전체의 판단 기준)
개발 내내 사용자가 세운 원칙. 개별 결정은 전부 이 원칙에서 파생됨.

1. **검증가능성·재현성이 최우선(북극성)** — "LLM 프롬프트로 행동 강제"는 재현·검증 불가라 지양. 가능한 한 **code-driven / logic-base**로.
2. **Tool vs Logic 분리** — 외부 자원(DB/API/LLM)에 닿거나 provenance가 필요하면 **Tool**, 순수 결정·조립은 **Logic**.
3. **Internal data = system of record, External = grounding/evidence** — cn_table·TARIC 테이블이 권위, 백과사전·LLM은 근거일 뿐.
4. **비정상성(non-stationary) 회피** — TARIC은 수시 변경 → 분류 지식을 weight에 굽지 말고 **갱신가능한 데이터**에.
5. **DPO/자가학습은 시기상조** — 라벨 전무 + 비정상 + 호스팅 모델 → **precedent 데이터 누적**으로 대체.

---

## 1. (6/15~6/18) 기반 진단 · 멀티에이전트 골격
### 의도
한글 입력으로 EU HS/TARIC을 분류하는데 정답률이 낮았음. 원인을 구조적으로 파악하고, monster orchestrator를 해체해 역할 분리된 멀티에이전트로 가고 싶었음.
### 방법
- scope 라우팅 버그픽스(화장품→cosmetics_33), domain 게이트 토글, A/B 하니스 구축.
- 9 의미그룹 + chapter pre-filter routing + Blackboard Rules + mini-agent 아키텍처 안 수립.
### 결과
- retriever 어휘 한계(영문 cn_table nomenclature) 진단. 엔진 승격은 A/B 결과까지 보류.
- Blackboard 기반 6-agent 파이프라인(EvidenceIntake→ProductUnderstanding→DomainRouter→Classification→Document→Orchestrator) 골격 확정.

## 2. (6/24) 입력 품질 병목 규명 + B-2 번역
### 의도
"정답률 진짜 병목이 뭔가"를 데이터로 확정하고 싶었음. retrieval 방법(의미검색 on/off)이 아니라 **입력 품질(한글 vs 영문 nomenclature)**이 병목이라는 가설.
### 방법
- 실험: clean 영어 32% vs clean 한글 3.2% vs noisy kurly 1.9% → **입력품질이 병목** 확증.
- **B-2**: ProductUnderstanding에 gemma3:4b로 한글→TARIC nomenclature 영어 번역 추가. 트리 traversal은 무거운 ctx 모델(Classifier) 몫으로 분리.
- 테스트셋 60/20/20 분할.
### 결과
- 번역만으로 test top-8: 휴리스틱 9.8→16.8%, 의미검색 1.9→17.3%. clean-input 36%.
- **결론: 병목 = 입력이 nomenclature 어휘를 안 담음.** 이후 모든 작업의 출발점.

## 3. (6/24) WCO 9-group 라우팅 백본
### 의도
scope 라우팅이 silent misrouting(엉뚱한 챕터로 조용히 감)하는 걸 결정론으로 막고, 챕터→그룹을 완전분할하고 싶었음.
### 방법
- `_external_classifier.py`에 **WCO_GROUP_CHAPTERS**(ch01-97 완전분할 9그룹) + **DEMO_BUCKET_CHAPTERS**(food_16_21, cosmetics) 도입. 챕터→그룹 결정론 매핑.
- 분류=WCO그룹 / 서류=규제scope 공존.
### 결과
- 그룹적중 29→89/96, 커버 97/97, silent misrouting 해소. stage1 미수정, 2파일만 변경.

## 4. (6/25~6/26) 데이터층 검증 · 금지 시드
### 의도
"에이전트 말고 Supabase 데이터 자체가 정확한가"를 전수 검증하고 싶었음(post/pre_taric에 declarable TARIC10이 다 있나, 서류가 EU 기준으로 맞나).
### 방법
- psql 직접 접속 + EUR-Lex/Access2Markets 교차검증.
- **금지 시드**: post_taric_requirement_master에 `POSTREQ_PROHIB_M277`(measure 277 금지카드) 1행 추가 = **데이터-only로 document_view 보강, 코드 0줄**.
### 결과
- declarable TARIC10 16,612 커버리지 완전(갭 0). "7,529 no-post"는 갭 아님(EU 교차검증: 추가서류 실제로 없음).
- FLEGT는 라이선스라 post 정상, 금지 제재는 pre-gate 정상. **bti_case=빈 테이블** 발견(후에 precedent 자리로).
- baseline 6종은 해결코드 항상표시(98/98) 확인.

## 5. (6월말) DTO 계약화 · 온톨로지 소비 논쟁
### 의도
ProductUnderstanding이 만드는 산출물(ProductFacts_dto)을 계약(contract)으로 굳히고, **온톨로지를 "LLM 조종용 산문"이 아니라 "코드가 실행하는 규칙"으로** 쓰고 싶었음. "md로 LLM 행동 강제"는 재현 불가라는 문제의식.
### 방법
- `dto.py` 생성: `ProductFacts_dto` 빌더 + 전체 DTO 체인(Route/Classify/Doc*…) 스캐폴딩. object_type 호환 유지.
- 온톨로지(ASAP_Ontology: stage_contract/profiles/tables) 분석 → **"기존 구조는 문서형 명세라 런타임 read-model엔 안 맞음"** 결론. Trigger Map/OntologyReadTool 층이 필요.
### 결과
- **핵심 통찰 확립**: 온톨로지의 학술적 가치 = *형식적·기계검증가능한 의미론*. 그러니 code-driven이 온톨로지를 *제대로* 쓰는 길. 단 관세분류는 부분적으로만 형식화 가능(GIR 해석은 LLM 필요).
- ProductFacts_dto가 stage_contract 스키마(ingredient_class/food_form enum)에서 이탈해 있음을 발견 → 정렬 필요.

## 6. (6월말~7월초) 2-lane DTO
### 의도
chapter는 "무엇인가(identity)", cn8은 "조성·함량(composition)"으로 판별축이 다름. **제품명→identity, OCR원재료→composition**으로 소스가 갈리니, DTO를 두 lane으로 나눠 **classifier가 projection만 읽게** 해 context를 줄이고 **chapter 라우팅에서 composition 노이즈를 빼고** 싶었음.
### 방법
- `ProductFacts_dto` 내부를 `identity_lane` / `composition_lane` / `classifier_projection`으로 재구성(물리적 분리 아닌 lane + projection).
- 비대칭 설계: **identity는 두 단계 모두, composition은 cn8로만**. allergen은 composition_lane에 *제외근거로만*.
### 결과
- 실측(멘보샤): 두 lane + projection + allergen 격리 정상 작동. DomainRouter가 identity projection만 읽어 chapter 정확(ch16).
- HS 트리 그라디언트(위=identity, 아래=composition)와 lane이 정합함을 확인.

## 7. (7월초) 백과사전(네이버) Tool — 외부지식 grounding
### 의도
"제품명이 주는 정보가 최대치"라 판단 → 마지막 방법으로 **백과사전 설명이 실제 nomenclature와 register가 맞다**는 가설. LLM 번역이 환각(재첩→"Jeongpyeon Guk")하는 걸, **권위 있는 entity 조회로 grounding**하고 싶었음.
### 방법
- `tools/encyclopedia_lookup.py` 신설 = **Tool**(외부 API + provenance). 네이버 `/v1/search/encyc.json`, HTML 제거, 캐시, graceful, `.provenance()` 반환.
- `.env`에 NAVER_CLIENT_ID/SECRET. ProductUnderstanding이 head-noun으로 호출 → identity_lane에 저장.
- **dirty context 해결**: 네이버 원문(조리법 등)을 직접 매칭하지 않고 **gemini가 증류(distill)** → ingredient_class/food_form enum + 1구문. raw는 evidence로만 격리.
- **재현성**: LLM 증류 결과를 캐시 → 같은 입력=같은 lookup(비결정성을 first-touch에 동결).
### 결과
- 검증(Fish cake→"minced fish, fried"=ch16, 재첩→"clam,mollusc"): 설명이 nomenclature와 정확히 맞음.
- Tool이 code-driven(코드가 호출, native tool-calling 불요)이라 모델 교체와 무관.
- 한계: head-noun 추출이 아직 풀네임을 넘기는 케이스(멘보샤→레시피) 존재, gemini가 제품명으로 보완.

## 8. (7월초) LLM 교체: gemma3:4b → gemini-2.5-flash
### 의도
`.env`를 Gemini로 바꾸자 ProductUnderstanding 번역이 빈 DTO를 냄(gemini-provider에 gemma3-model 억지 결합). 번역 품질도 gemma3가 약함(재첩→"Mud snails").
### 방법
- 번역/증류를 gemini-2.5-flash로. 키는 `_read_gemini_api_key()` 경로 재사용, OpenAI-compat endpoint 직접 호출.
- gemini의 native tool-calling 지원 확인(테스트: encyclopedia_lookup tool_call 정상 반환)했으나, **code-driven 유지**(재현성).
### 결과
- 번역 품질 급상승: 낙지볶음→"Octopus, stir-fried, seasoned", 어묵→"Fish paste, processed, cooked". nomenclature register 정확.

## 9. (7월초) ★ Staged Classification — 분류기 재설계 (핵심 성과)
### 의도
기존 classifier(llm_classifier)가 **full cn_table(9762행) 한 방 검색** → 후보 점수가 촘촘(0.45/0.42)해 랭킹 불안정, 발산(멘보샤/주꾸미 ch16→ch19 빵), cn8=0%. **HS 트리를 인간처럼 chapter→hs4→hs6→cn8로 좁혀가며** 각 레벨에서 ProductFacts의 해당 축을 매칭하고 싶었음. 단 **chapter를 hard-lock하면 backtracking이 죽으니** 가설로 두고.
### 방법
- **AXIS_MAP**: 판별축(ingredient_taxonomy/product_form/processing_state/composition_percentage/packaging) → ProductFacts 필드 경로 매핑. "레벨별 하드코딩이 아니라 노드 discriminator가 읽을 필드를 결정".
- `tools/staged_classification.py` 재작성(334→732줄): `classify()`가 **hs4 → (선택 hs4의 children) hs6 → (선택 hs6의 children) cn8**로 `parent_codes` 필터링하며 narrowing. 레벨마다 결정론(lexical + `_embed`/`_cosine`) rank → LLM select → 실패 시 결정론 fallback.
- **관계 역전**: llm_classifier를 통짜 분류기가 아니라 **노드 판정 프리미티브**(_load_cn_rows/_embed/llm)로 강등, staged tool이 순회 주도(controller-driven).
- Classify_dto를 **HS4/HS6/CN8Classify_dto 3단계**로 분할, blackboard에 `classification_stage_results` 트레이스(각 stage의 decision_axes/facts_used).
### 결과
- **3건 샘플: chapter 3/3, hs6_top1 3/3, hs6_recall@5 3/3, cn8 3/3 (전부 100%)**.
- 이전 발산(주꾸미 ch16→ch19)·**cn8=0%가 동시 해소**(주꾸미 cn8=16055500 정답).
- 1샘플 정합성 검사: ProductFacts 생성 근거 + classification 소비(hs4`['1605','2005','2103']`→hs6`['160555']`→cn8`['16055500']`) 트레이스 확인. 덤프: `artifacts/pipeline_smoke/run_dump/`.
- **미완**: `missing_facts`는 아직 `[]` stub, cn8 축(composition%/packaging) 미구현, projection 슬림화·conflict 신호·precedent 미구현.

## 10. (7/1) 도식화 · 개발 문서
### 의도
현재 구조와 진행 방향을 한눈에 보고 관리하고 싶었음.
### 방법
- `docs/pipeline_current_vs_proposed.md`(mermaid: 현 파이프라인 + DTO 흐름 + 미구현 제안 목록).
- `docs/pipeline.excalidraw`(분류 spine + 서류 4층 시각화: baseline/pre=Route, post=TARIC10).
### 결과
- 현 구조(staged 반영) + 다음 작업(missing_facts/conflict/projection/precedent) 문서화.

---

## 11. 결정했으나 *의도적으로 보류*한 것 (근거 포함)
- **DPO/자가학습**: 라벨 전무 + TARIC 비정상성 + gemini 호스팅(파인튜닝 불가) → **precedent 테이블(bti_case) 누적**으로 대체. 애매성은 결정론 conflict 신호로 감지, 사람/판례로 해소.
- **chapter hard-lock**: backtracking을 죽이므로 금지. 대신 "chapter=가설, classifier 불일치=명시 conflict 신호".
- **프롬프트 보강**: 최후의 보루. context 축소는 프롬프트가 아니라 **projection(코드)**로.
- **WCO 전체 활성화**(DEMO_ONLY=0): 후보공간이 넓어져 오히려 정답률 하락(chapter 67→46%, recall@5 50→17%) → **진짜 narrowing(staged) 전까진 demo-only 유지** 판단.

## 12. 현재 상태 요약 (2026-07-01)
| 계층 | 상태 |
|---|---|
| 6-agent 골격 | ✅ end-to-end 완주 |
| ProductFacts_dto 2-lane + 네이버 grounding + gemini 증류 | ✅ 구현·검증 |
| Staged Classification (hs4→hs6→cn8 narrowing) | ✅ 구현, 3건 100% (풀 48건 검증 진행 중) |
| Document 4층(baseline/pre/post/binding) | ✅ 성숙(데이터 검증 완료) |
| missing_facts / conflict 신호 / projection 슬림화 / precedent(bti_case) | ❌ 다음 작업 |

**한 줄**: 6/15 "한글 입력 저정확도 진단"에서 출발해, **입력품질(번역)→라우팅(WCO)→데이터검증→DTO 계약화(2-lane)→외부지식 grounding(네이버)→분류기 재설계(staged narrowing)**로 병목을 단계적으로 하류로 밀어냈고, cn8=0%→3/3까지 도달. 남은 병목은 cn8 세부축·재현성 마감(missing_facts/conflict/precedent).

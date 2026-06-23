# classifier_ab_smoke — 두 분류기 A/B 비교 핸드오프

retriever(키워드 온톨로지) ↔ llm engine(pgvector+EBTI+Ollama gemma judge)를 **같은 입력**
(URL → `KurlyProductPipeline` → 구조화 facts, 선택적 풀 OCR)으로 돌려 HS6 정답률을 비교한다.
임시 배선이며 **아직 주 파이프라인에 승격(통합) 안 함** — 이 비교 결과 보고 승격 여부 결정.

## 인터프리터
반드시 conda `asap` env 사용 (psycopg2 / sentence_transformers / playwright / paddleocr 포함):
```
/opt/anaconda3/envs/asap/bin/python
```

## 외부 의존(떠 있어야 함)
- Ollama @ localhost:11434 — `gemma4:26b`(분류/번역), `gemma3:4b`(judge)
- PostgreSQL `asap_db` — taric_nomenclature(pgvector) / eu_bti / cn_explanatory_notes 등

## 실행
```bash
A=/opt/anaconda3/envs/asap/bin/python

# 1) retriever 백엔드 (기본). 게이트 해제로 전 chapter 후보 허용:
CLASSIFIER_BACKEND=retriever ASAP_DISABLE_DOMAIN_SCOPE_GATE=1 $A classifier_ab_smoke.py

# 2) llm 엔진 백엔드 + 풀 OCR(성분표까지):
CLASSIFIER_BACKEND=llm ASAP_AB_RUN_OCR=1 $A classifier_ab_smoke.py

# 빠른 확인: 앞 N건만
ASAP_AB_LIMIT=3 CLASSIFIER_BACKEND=llm $A classifier_ab_smoke.py
```

### env 토글
| env | 의미 | 기본 |
|---|---|---|
| `CLASSIFIER_BACKEND` | `retriever` \| `llm` | retriever |
| `ASAP_AB_RUN_OCR` | 풀 OCR(PaddleStructure) 켜기 (느림) | off |
| `ASAP_DISABLE_DOMAIN_SCOPE_GATE` | retriever chapter 16-21/33 게이트 해제 | off |
| `ASAP_AB_LIMIT` | 앞 N개 URL만 | 0(전체) |

## 출력
`artifacts/classifier-ab/ab-<backend>-summary.json` — backend별로 분리 저장(서로 안 덮어씀).
각 레코드: us_hs6 / our_hs6(top5) / top1_match / in_top5 / candidates / meta / elapsed.
콘솔 요약: `[backend] total=.. Top1=x/n Top5=y/n 후보없음=.. 에러=..`

## 알려진 한계 (결과 해석 시 주의)
1. **정답 라벨이 미국 HS** → 국제공통 **HS6(앞6자리)** 단위로만 비교(EU-CN 정답 아님).
2. **스크래핑**: kurlyglobal이 간헐적으로 Shopify password/coming-soon 게이트 페이지를 줌
   (반복요청 시 레이트리밋). 그 경우 입력이 오염되어 분류가 무의미해짐 → 결과에서
   product_name이 "비밀번호를 입력해…"인 행은 제외하고 봐야 함. **스크래핑은 현재 수준에서만
   검증**(web_parser 수정 금지).
3. retriever는 짧은 입력에 후보를 거의 못 냄(기존 약점). llm 엔진은 무관.

## 수정 금지 경계
- `src/eu_export/`는 손대지 말 것(web_parser·ocr 포함). **예외: stage1.py의 domain 게이트
  토글만 허용**(이미 적용됨).
- 이 비교는 평가용이며, llm 엔진을 `_external_classifier` 등 주 경로에 통합하지 말 것
  (승격 보류 상태).

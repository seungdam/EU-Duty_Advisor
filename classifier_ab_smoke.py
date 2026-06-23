"""두 분류기(retriever ↔ llm engine)를 같은 입력으로 A/B 비교하는 smoke.

설계:
  URL → KurlyProductPipeline(+선택적 풀 OCR)  ← 공유 front-end (입력 품질 동일하게)
      → 구조화 product facts (상품명 + 정규화 fact + OCR 성분표 텍스트)
      → backend 디스패치:
           retriever : eu_export.core.CnCandidateRetriever (키워드, 게이트 env로 해제 가능)
           llm       : engine.classifier.classify (pgvector + EBTI + Ollama gemma judge)
      → CN8/HS6 후보 → US HS6(참조)와 HS6 단위 비교 → 정답률.

토글:
  CLASSIFIER_BACKEND=retriever|llm   (기본 retriever)
  ASAP_AB_RUN_OCR=1                  (풀 OCR 켜기, 기본 끔 — 켜면 느림)
  ASAP_DISABLE_DOMAIN_SCOPE_GATE=1   (retriever chapter 게이트 해제)
  ASAP_AB_LIMIT=N                    (앞 N개 URL만)

사용: /opt/anaconda3/envs/asap/bin/python classifier_ab_smoke.py [dataset.csv]
"""

import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

PROJECT_ROOT_PATH = Path(__file__).resolve().parent
SOURCE_ROOT_PATH = PROJECT_ROOT_PATH / "src"
for extraPath in (PROJECT_ROOT_PATH, SOURCE_ROOT_PATH):
    extraPathText = str(extraPath)
    if extraPathText in sys.path:
        sys.path.remove(extraPathText)
if PROJECT_ROOT_PATH.exists():
    sys.path.insert(0, str(PROJECT_ROOT_PATH))
if SOURCE_ROOT_PATH.exists():
    sys.path.insert(0, str(SOURCE_ROOT_PATH))

DEFAULT_DATASET_PATH = PROJECT_ROOT_PATH / "(캡스톤) HS Code 데이터쌍_식품상세반영.csv"
ARTIFACT_DIR = PROJECT_ROOT_PATH / "artifacts" / "classifier-ab"
INPUT_DICTIONARY_PATH = (
    SOURCE_ROOT_PATH
    / "bussiness_logic"
    / "input_process"
    / "resources"
    / "product_input_dictionary.csv"
)

BACKEND = os.environ.get("CLASSIFIER_BACKEND", "retriever").strip().lower()
RUN_OCR = os.environ.get("ASAP_AB_RUN_OCR", "").strip().lower() in {"1", "true", "yes", "on"}
URL_LIMIT = int(os.environ.get("ASAP_AB_LIMIT", "0") or "0")
DOMAIN_SCOPE = "food_16_21"
TOP_K = 5
FACT_TEXT_LIMIT = 1500


class ClassifierAbSmoke:
    def __init__(self, datasetPath: Path) -> None:
        self._datasetPath = datasetPath
        self._pipeline = None
        self._retriever = None
        self._engineClassify = None

    # ── 입력 공유 front-end ────────────────────────────────────
    def _GetPipeline(self):
        if self._pipeline is None:
            from bussiness_logic.product import (
                KurlyGlobalPageParser,
                KurlyDomesticPageParser,
                KurlyPageAdapter,
                KurlyPageCollector,
                KurlyProductPipeline,
                PaddleStructureOcrEngine,
            )
            from bussiness_logic.input_process import ProductInputReconstructionService

            adapter = KurlyPageAdapter(
                domesticParser=KurlyDomesticPageParser(),
                globalParser=KurlyGlobalPageParser(),
            )
            collector = KurlyPageCollector(
                parser=adapter,
                headless=True,
                timeoutMilliseconds=30000,
                scrollCount=3,
            )
            reconstruction = ProductInputReconstructionService(
                dictionaryPath=(
                    str(INPUT_DICTIONARY_PATH)
                    if INPUT_DICTIONARY_PATH.exists()
                    else None
                ),
                runtimeAdapter=None,
            )
            kwargs: Dict[str, Any] = {
                "collector": collector,
                "inputReconstructionService": reconstruction,
            }
            if RUN_OCR:
                kwargs["ocrEngine"] = PaddleStructureOcrEngine()
            self._pipeline = KurlyProductPipeline(**kwargs)
        return self._pipeline

    def _CollectInput(self, url: str) -> Tuple[str, str, Dict[str, Any]]:
        from bussiness_logic.product import KurlyPipelineInput

        result = self._GetPipeline().Run(
            KurlyPipelineInput(
                productPageUrl=url,
                runOcrFallback=RUN_OCR,
                artifactRootPath=ARTIFACT_DIR / "ocr",
                maxOcrImageCount=20,
            )
        )
        parsed = result.collectionResult.parsedProductPage
        productName = (parsed.productName or "").strip()
        factTexts = list(result.inputReconstructionResult.normalizedFactTexts)
        if not factTexts:
            factTexts = list(result.ocrNormalizationResult.factTexts)
        combinedOcr = (result.combinedOcrText or "").strip()
        noticeText = "\n".join(
            f"{(f.fieldName or '').strip()}: {(f.fieldValue or '').strip()}"
            for f in parsed.productNoticeFields
            if (f.fieldValue or "").strip()
        )
        visibleText = ""
        if result.renderedPageEvidence is not None:
            visibleText = (result.renderedPageEvidence.visibleText or "").strip()

        # 엔진/리트리버 공용 입력 텍스트: fact > 상품고시 > OCR > visible 순으로 합침
        parts = [p for p in (factTexts and "\n".join(factTexts) or "", noticeText, combinedOcr, visibleText) if p]
        compositionText = "\n".join(parts)[:FACT_TEXT_LIMIT]
        meta = {
            "requires_ocr_fallback": parsed.requiresOcrFallback,
            "fact_count": len(factTexts),
            "notice_field_count": len(parsed.productNoticeFields),
            "ocr_text_length": len(combinedOcr),
            "visible_text_length": len(visibleText),
        }
        return productName, compositionText, meta

    # ── backend: retriever ────────────────────────────────────
    def _ClassifyRetriever(self, name: str, composition: str) -> List[Dict[str, Any]]:
        if self._retriever is None:
            from agents._external_classifier import ASAP_ONTOLOGY_ROOT, ASAP_PROJECT_ROOT
            from bussiness_logic.core import CnCandidateRetriever

            self._retriever = CnCandidateRetriever(ASAP_ONTOLOGY_ROOT, ASAP_PROJECT_ROOT)
        from agents._external_classifier import pes_to_input

        pes = {
            "observed_facts": {
                "product_name": name,
                "description": name,
                "composition": [composition] if composition else [],
            }
        }
        productInput = pes_to_input(pes, domain_scope=DOMAIN_SCOPE)
        candidates = self._retriever.FindCandidates(productInput, topK=TOP_K)
        return [
            {"cn8": c.hs8, "hs6": c.hs8[:6], "score": round(float(c.score), 3)}
            for c in candidates
        ]

    # ── backend: llm engine ───────────────────────────────────
    def _ClassifyLlm(self, name: str, composition: str) -> List[Dict[str, Any]]:
        if self._engineClassify is None:
            from agents.llm_classifier import classify as engineClassify

            self._engineClassify = engineClassify
        productInput = (name + "\n" + composition).strip()[:600] or name
        rows = self._engineClassify(productInput, top_k=TOP_K)
        out: List[Dict[str, Any]] = []
        for r in rows:
            taric = str(r.get("결정세번", "") or "")
            hs6 = str(r.get("hs6", "") or "")
            out.append(
                {
                    "cn8": taric[:8],
                    "hs6": hs6,
                    "score": round(float(r.get("신뢰도") or 0.0), 3),
                    "status": r.get("검증_상태", ""),
                    "stage": r.get("분류_단계", ""),
                }
            )
        return out

    def _Classify(self, name: str, composition: str) -> List[Dict[str, Any]]:
        if BACKEND == "llm":
            return self._ClassifyLlm(name, composition)
        return self._ClassifyRetriever(name, composition)

    # ── runner ────────────────────────────────────────────────
    def Run(self) -> int:
        self._ConfigureLogger()
        log = logger.bind(className="ClassifierAbSmoke", functionName="Run")
        rows = self._ReadDataset()
        if URL_LIMIT > 0:
            rows = rows[:URL_LIMIT]
        log.info(
            "backend={} run_ocr={} gate_disabled={} urls={}",
            BACKEND,
            RUN_OCR,
            os.environ.get("ASAP_DISABLE_DOMAIN_SCOPE_GATE", ""),
            len(rows),
        )

        results: List[Dict[str, Any]] = []
        for i, row in enumerate(rows, 1):
            started = time.monotonic()
            try:
                name, composition, meta = self._CollectInput(row["url"])
                if not (name or "").strip():
                    candidates = []
                    err = "product_name_not_collected"
                    meta = dict(meta)
                    meta["collection_failed"] = True
                else:
                    candidates = self._Classify(name, composition)
                    err = None
            except Exception as error:  # noqa: BLE001
                name, composition, meta, candidates = "", "", {}, []
                err = str(error)
            elapsed = round(time.monotonic() - started, 2)

            us6 = self._Us6(row["us_hs"])
            our6 = [c["hs6"] for c in candidates]
            top1 = bool(our6) and our6[0] == us6 and bool(us6)
            top5 = us6 in our6 and bool(us6)
            log.info(
                "[{}/{}] {} | top1={} | our={} | US6={} ({}s){}",
                i, len(rows), (name or "(이름없음)")[:30],
                our6[0] if our6 else "-",
                " ".join(our6[:5]) or "-",
                us6 or "-",
                elapsed,
                f" ERR={err}" if err else "",
            )
            results.append(
                {
                    "index": i, "url": row["url"], "product_name": name,
                    "us_hs6": us6, "us_raw": row["us_hs"],
                    "candidates": candidates, "our_hs6": our6,
                    "top1_match": top1, "in_top5": top5,
                    "meta": meta, "elapsed_seconds": elapsed, "error": err,
                }
            )

        self._Summarize(results, log)
        self._WriteArtifact(results)
        return 0

    def _Summarize(self, results: List[Dict[str, Any]], log) -> None:
        n = len(results)
        scored = [r for r in results if r["us_hs6"]]
        t1 = sum(1 for r in results if r["top1_match"])
        t5 = sum(1 for r in results if r["in_top5"])
        empty = sum(1 for r in results if not r["our_hs6"])
        errs = sum(1 for r in results if r["error"])
        log.info(
            "[{}] total={} Top1={}/{} ({:.1f}%) Top5={}/{} ({:.1f}%) 후보없음={} 에러={}",
            BACKEND, n, t1, n, (t1 / n * 100 if n else 0),
            t5, n, (t5 / n * 100 if n else 0), empty, errs,
        )

    def _WriteArtifact(self, results: List[Dict[str, Any]]) -> None:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        path = ARTIFACT_DIR / f"ab-{BACKEND}-summary.json"
        n = len(results)
        payload = {
            "backend": BACKEND, "run_ocr": RUN_OCR,
            "gate_disabled": os.environ.get("ASAP_DISABLE_DOMAIN_SCOPE_GATE", ""),
            "total": n,
            "top1": sum(1 for r in results if r["top1_match"]),
            "top5": sum(1 for r in results if r["in_top5"]),
            "results": results,
        }
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.bind(className="ClassifierAbSmoke", functionName="_WriteArtifact").info(
            "artifact 저장 path={}", path
        )

    # ── helpers ───────────────────────────────────────────────
    def _ReadDataset(self) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        with self._datasetPath.open("r", encoding="utf-8-sig", newline="") as f:
            for idx, row in enumerate(csv.reader(f)):
                if idx == 0 or not row:
                    continue
                url = row[0].strip()
                if not url.startswith("http"):
                    continue
                rows.append({"url": url, "us_hs": row[1].strip() if len(row) > 1 else ""})
        return rows

    @staticmethod
    def _Us6(raw: str) -> str:
        digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
        if 0 < len(digits) < 10:
            digits = digits.zfill(10)
        return digits[:6] if len(digits) >= 6 else ""

    @staticmethod
    def _ConfigureLogger() -> None:
        logger.remove()
        logger.add(
            sys.stderr,
            format="<level>[{level}]</level> <cyan>{extra[functionName]}: {message}</cyan>",
            level="INFO",
            colorize=True,
        )


if __name__ == "__main__":
    dataset = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else DEFAULT_DATASET_PATH
    sys.exit(ClassifierAbSmoke(dataset).Run())

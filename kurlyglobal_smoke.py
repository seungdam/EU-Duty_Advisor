"""KurlyGlobal 상품 URL → CN/HS 후보 코드 smoke.

CSV 데이터셋의 A열(상품 상세 URL)을 하나씩 Playwright로 렌더링하여 상품명/설명/본문
텍스트를 추출하고, 이를 ASAPExpress 분류 입력(ProductClassificationInput)으로 변환해
CnCandidateRetriever로 CN8/HS6 후보 코드를 산출한다.

이 smoke는 LLM 없이 동작하는 retriever 기반 후보 산출이다(run_002의 retriever fallback과
동일 경로). 최종 분류가 아니라 사람이 검토할 후보 목록이며, B열(미국 HS Code)은 참조용으로만
같이 출력한다(EU 정답 아님).
"""

import csv
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


PROJECT_ROOT_PATH = Path(__file__).resolve().parent
SOURCE_ROOT_PATH = PROJECT_ROOT_PATH / "src"
for extraPath in (SOURCE_ROOT_PATH, PROJECT_ROOT_PATH):
    if str(extraPath) not in sys.path:
        sys.path.insert(0, str(extraPath))

DEFAULT_DATASET_PATH = PROJECT_ROOT_PATH / "(캡스톤) HS Code 데이터쌍_식품상세반영.csv"
DEFAULT_SUMMARY_ARTIFACT_PATH = (
    PROJECT_ROOT_PATH / "artifacts" / "kurlyglobal-smoke" / "classification-smoke-summary.json"
)

PAGE_LOAD_TIMEOUT_MILLISECONDS = 30000
DOMAIN_SCOPE = "food_16_21"  # 데이터셋이 전부 식품이므로 food scope 고정
TOP_K = 5
BODY_TEXT_LIMIT = 2000
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class KurlyGlobalClassificationSmokeRunner:
    """KurlyGlobal 상품 URL을 분류 파이프라인에 넣어 CN/HS 후보 코드를 산출한다."""

    def __init__(self, datasetPath: Path, summaryArtifactPath: Path) -> None:
        self._datasetPath = datasetPath
        self._summaryArtifactPath = summaryArtifactPath
        self._retriever = None  # lazy init (import 비용 회피)

    def Run(self) -> int:
        self._ConfigureLogger()
        runLogger = self._Logger("Run")

        runLogger.info("STEP 1/3 데이터셋에서 상품 URL을 읽습니다 path={}", self._datasetPath)
        rows = self._ReadDatasetRows()
        if not rows:
            runLogger.error("데이터셋에서 유효한 URL을 찾지 못했습니다. 중단합니다.")
            return 1
        runLogger.info("URL {}개를 로드했습니다", len(rows))

        runLogger.info("STEP 2/3 각 URL을 수집하고 CN/HS 후보를 분류합니다 url_count={}", len(rows))
        results = self._ClassifyAll(rows)

        runLogger.info("STEP 3/3 결과를 요약하고 artifact를 저장합니다")
        self._LogSummary(results)
        self._WriteSummaryArtifact(results)

        failureCount = sum(1 for item in results if item["error"])
        return 1 if failureCount else 0

    def _ReadDatasetRows(self) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        with self._datasetPath.open("r", encoding="utf-8-sig", newline="") as datasetFile:
            reader = csv.reader(datasetFile)
            for rowIndex, row in enumerate(reader):
                if rowIndex == 0 or not row:
                    continue  # 헤더(상품 상세, 미국 HS Code) 또는 빈 줄
                productUrl = row[0].strip()
                if not productUrl.startswith("http"):
                    continue
                usHsCode = row[1].strip() if len(row) > 1 else ""
                rows.append({"url": productUrl, "us_hs_code_reference": usHsCode})
        return rows

    def _ClassifyAll(self, rows: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        from playwright.sync_api import sync_playwright

        results: List[Dict[str, Any]] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(user_agent=USER_AGENT)
                context.route("**/*", self._BlockUnnecessaryResource)
                try:
                    for productIndex, row in enumerate(rows, start=1):
                        result = self._ClassifyOne(context, productIndex, len(rows), row)
                        results.append(result)
                finally:
                    context.close()
            finally:
                browser.close()
        return results

    def _ClassifyOne(
        self,
        context: Any,
        productIndex: int,
        productCount: int,
        row: Dict[str, str],
    ) -> Dict[str, Any]:
        runLogger = self._Logger("_ClassifyOne")
        productUrl = row["url"]
        startedAt = time.monotonic()
        page = context.new_page()
        try:
            page.goto(productUrl, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MILLISECONDS)
            productName = self._ReadFirstHeading(page)
            pageTitle = (page.title() or "").strip()
            bodyText = page.inner_text("body").strip()

            candidates = self._Classify(productName, pageTitle, bodyText)
            elapsedSeconds = round(time.monotonic() - startedAt, 2)
            topLine = (
                "  ".join(f"{c['cn8']}({c['score']})" for c in candidates[:3])
                if candidates
                else "(후보 없음)"
            )
            runLogger.info(
                "[{}/{}] {} | top: {}  (US참조={}) ({}s)",
                productIndex,
                productCount,
                productName[:34],
                topLine,
                row["us_hs_code_reference"] or "-",
                elapsedSeconds,
            )
            return {
                "index": productIndex,
                "url": productUrl,
                "product_name": productName,
                "page_title": pageTitle,
                "us_hs_code_reference": row["us_hs_code_reference"],
                "candidate_count": len(candidates),
                "candidates": candidates,
                "elapsed_seconds": elapsedSeconds,
                "error": None,
            }
        except Exception as error:  # noqa: BLE001 - smoke는 모든 실패를 기록한다
            elapsedSeconds = round(time.monotonic() - startedAt, 2)
            runLogger.warning(
                "[{}/{}] FAIL url={} error={} ({}s)",
                productIndex,
                productCount,
                productUrl,
                error,
                elapsedSeconds,
            )
            return {
                "index": productIndex,
                "url": productUrl,
                "product_name": "",
                "page_title": "",
                "us_hs_code_reference": row["us_hs_code_reference"],
                "candidate_count": 0,
                "candidates": [],
                "elapsed_seconds": elapsedSeconds,
                "error": str(error),
            }
        finally:
            page.close()

    def _Classify(self, productName: str, pageTitle: str, bodyText: str) -> List[Dict[str, Any]]:
        from agents._external_classifier import pes_to_input

        productEvidenceState = {
            "observed_facts": {
                "product_name": productName,
                "description": pageTitle,
                "composition": [bodyText[:BODY_TEXT_LIMIT]] if bodyText else [],
            }
        }
        productInput = pes_to_input(productEvidenceState, domain_scope=DOMAIN_SCOPE)
        candidates = self._GetRetriever().FindCandidates(productInput, topK=TOP_K)
        return [
            {
                "cn8": candidate.hs8,
                "hs6": candidate.hs8[:6],
                "score": round(candidate.score, 2),
                "matched_terms": list(candidate.matchedTerms[:8]),
            }
            for candidate in candidates
        ]

    def _GetRetriever(self) -> Any:
        if self._retriever is None:
            from agents._external_classifier import ASAP_ONTOLOGY_ROOT, ASAP_PROJECT_ROOT
            from bussiness_logic.core import CnCandidateRetriever

            self._retriever = CnCandidateRetriever(ASAP_ONTOLOGY_ROOT, ASAP_PROJECT_ROOT)
        return self._retriever

    def _ReadFirstHeading(self, page: Any) -> str:
        try:
            locator = page.locator("h1").first
            if locator.count() > 0:
                return (locator.inner_text(timeout=2000) or "").strip()
        except Exception:  # noqa: BLE001
            return ""
        return ""

    def _BlockUnnecessaryResource(self, route: Any) -> None:
        if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
            route.abort()
            return
        route.continue_()

    def _LogSummary(self, results: List[Dict[str, Any]]) -> None:
        runLogger = self._Logger("_LogSummary")
        total = len(results)
        errorCount = sum(1 for item in results if item["error"])
        withCandidates = sum(1 for item in results if item["candidate_count"] > 0)
        runLogger.info(
            "요약: total={} 분류성공(후보있음)={} 후보없음={} 수집실패={}",
            total,
            withCandidates,
            total - withCandidates - errorCount,
            errorCount,
        )
        for item in results:
            if item["error"]:
                runLogger.warning("실패 index={} url={} error={}", item["index"], item["url"], item["error"])
            elif item["candidate_count"] == 0:
                runLogger.warning("후보 없음 index={} product={}", item["index"], item["product_name"])

    def _WriteSummaryArtifact(self, results: List[Dict[str, Any]]) -> None:
        runLogger = self._Logger("_WriteSummaryArtifact")
        total = len(results)
        summary = {
            "dataset_path": str(self._datasetPath),
            "domain_scope": DOMAIN_SCOPE,
            "top_k": TOP_K,
            "total": total,
            "with_candidates": sum(1 for item in results if item["candidate_count"] > 0),
            "errors": sum(1 for item in results if item["error"]),
            "results": results,
        }
        self._summaryArtifactPath.parent.mkdir(parents=True, exist_ok=True)
        with self._summaryArtifactPath.open("w", encoding="utf-8") as artifactFile:
            json.dump(summary, artifactFile, ensure_ascii=False, indent=2)
        runLogger.info("artifact 저장 완료 path={}", self._summaryArtifactPath)

    def _ConfigureLogger(self) -> None:
        logger.remove()
        logger.add(
            sys.stderr,
            format=(
                "<level>[{level}]</level> "
                "<cyan>{extra[className]}::{extra[functionName]}: {message}</cyan>"
            ),
            level="INFO",
            colorize=True,
        )

    def _Logger(self, functionName: str) -> Any:
        return logger.bind(className=self.__class__.__name__, functionName=functionName)


if __name__ == "__main__":
    datasetPath = (
        Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else DEFAULT_DATASET_PATH
    )
    runner = KurlyGlobalClassificationSmokeRunner(datasetPath, DEFAULT_SUMMARY_ARTIFACT_PATH)
    sys.exit(runner.Run())
